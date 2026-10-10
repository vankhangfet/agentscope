# -*- coding: utf-8 -*-
"""Workflow run lifecycle endpoints over a real app and a fake model.

The chat path is faked at the credential layer: a custom credential
registered with ``CredentialFactory`` whose model class replies
instantly. Runs therefore exercise the whole stack — router, service,
engine, chat service, storage — with no network.
"""
import tempfile
import time
from typing import Any, AsyncGenerator, Literal, Type
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import ToolChoice

HEADERS = {"X-User-ID": "alice"}


class _FakeRouterCredential(CredentialBase):
    """A credential whose chat model answers instantly, no network."""

    type: Literal["fake_router_credential"] = "fake_router_credential"
    """The credential type discriminator."""

    @classmethod
    def get_chat_model_class(cls) -> Type[ChatModelBase]:
        """Return the fake model class this credential resolves to."""
        return _FakeRouterModel


class _FakeRouterModel(ChatModelBase):
    """A model that replies instantly, or fails when scripted to.

    A model name starting with ``fail-`` makes every call raise, which
    surfaces through the chat turn as an error-bearing reply and so
    fails the node under the engine's retry budget.
    """

    def __init__(
        self,
        credential: CredentialBase,
        model: str,
        parameters: BaseModel | None = None,
        stream: bool = False,
        max_retries: int = 0,
        retry_delay: float = 0.0,
        context_size: int = 32768,
    ) -> None:
        """Initialize like a provider model, minus the HTTP client."""
        super().__init__(
            credential=credential,
            model=model,
            parameters=parameters or self.Parameters(),
            stream=stream,
            max_retries=max_retries,
            retry_delay=retry_delay,
            context_size=context_size,
        )
        # The agent reads ``formatter.supported_input_media_types`` on
        # every incoming message, so mirror real providers.
        self.formatter = OpenAIChatFormatter()

    @classmethod
    def list_models(cls, *args: Any, **kwargs: Any) -> list:
        """No model cards: keeps resolution hermetic to this test."""
        return []

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """Answer with one text block, or raise when scripted to fail."""
        del messages, tools, tool_choice, kwargs
        if model_name.startswith("fail-"):
            raise RuntimeError("fake model is scripted to fail")
        return ChatResponse(
            content=[TextBlock(text=f"FAKE-OK:{model_name}")],
            is_last=True,
        )


CredentialFactory.register_credential(_FakeRouterCredential)


def _fake_backends() -> tuple:
    """Build a fakeredis-backed storage and message bus."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _Storage(RedisStorage):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    class _Bus(RedisMessageBus):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    return _Storage(), _Bus()


def _graph(agent_id: str) -> dict:
    """An approval gate fanning approve/reject into two agent nodes.

    Both successors are terminal, so the run finishes along whichever
    branch the verdict picked and finalization cancels the other.
    """
    return {
        "nodes": [
            {"id": "gate", "config": {"type": "human_approval"}},
            {"id": "ok", "config": {"type": "agent", "agent_id": agent_id}},
            {"id": "no", "config": {"type": "agent", "agent_id": agent_id}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "gate",
                "target": "ok",
                "branch": "approve",
            },
            {
                "id": "e2",
                "source": "gate",
                "target": "no",
                "branch": "reject",
            },
        ],
    }


class WorkflowRunRouterTest(IsolatedAsyncioTestCase):
    """Start, verdict, cancel and retry a run over HTTP."""

    def setUp(self) -> None:
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        self._client = self.enterContext(
            TestClient(
                create_app(
                    storage=storage,
                    message_bus=bus,
                    workspace_manager=LocalWorkspaceManager(workdir),
                    enable_index_worker=False,
                ),
            ),
        )
        created = self._client.post(
            "/credential/",
            json={"data": {"type": "fake_router_credential", "name": "fake"}},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        self._credential_id = created.json()["credential_id"]
        self._agent_id = self._client.post(
            "/agent/",
            json={"name": "worker", "system_prompt": "hi"},
            headers=HEADERS,
        ).json()["agent_id"]
        workflow = self._client.post(
            "/workflow/",
            json={
                "data": {"name": "gate", "graph": _graph(self._agent_id)},
            },
            headers=HEADERS,
        )
        self.assertEqual(workflow.status_code, 201)
        self._workflow_id = workflow.json()["workflow_id"]

    # ------------------------------------------------------------- helpers

    def _model(self, name: str = "ok-model") -> dict:
        """The run-level model config resolving to the fake credential."""
        return {
            "type": "fake",
            "credential_id": self._credential_id,
            "model": name,
            "parameters": {},
        }

    def _start(self, model: str = "ok-model") -> str:
        """Start a run of the gate workflow and return its id."""
        started = self._client.post(
            f"/workflow/{self._workflow_id}/run",
            json={
                "input": {"topic": "tests"},
                "chat_model_config": self._model(model),
            },
            headers=HEADERS,
        )
        self.assertEqual(started.status_code, 201)
        return started.json()["run_id"]

    def _run_data(self, run_id: str) -> dict:
        """One run's persisted content."""
        fetched = self._client.get(
            f"/workflow/runs/{run_id}",
            headers=HEADERS,
        )
        self.assertEqual(fetched.status_code, 200)
        return fetched.json()["data"]

    def _poll(
        self,
        run_id: str,
        statuses: set[str],
        timeout: float = 10.0,
    ) -> dict:
        """Poll a run until it reaches one of *statuses*."""
        deadline = time.monotonic() + timeout
        data: dict | None = None
        while time.monotonic() < deadline:
            data = self._run_data(run_id)
            if data["status"] in statuses:
                return data
            time.sleep(0.05)
        self.fail(
            f"Run {run_id!r} never reached {statuses}; "
            f"last status {data and data['status']!r}.",
        )

    def _approve(
        self,
        run_id: str,
        node_id: str,
        verdict: str,
    ):
        """File a verdict on a run."""
        return self._client.post(
            f"/workflow/runs/{run_id}/approve",
            json={"node_id": node_id, "verdict": verdict, "note": ""},
            headers=HEADERS,
        )

    # --------------------------------------------------------------- tests

    def test_start_run_parks_on_approval(self) -> None:
        """A fresh run runs to the gate and parks WAITING_HUMAN."""
        run_id = self._start()
        parked = self._poll(run_id, {"WAITING_HUMAN"})
        self.assertEqual(
            parked["node_states"]["gate"]["status"],
            "WAITING_HUMAN",
        )
        # Neither successor has been touched: absent or still PENDING.
        for node_id in ("ok", "no"):
            state = parked["node_states"].get(node_id)
            self.assertTrue(
                state is None or state["status"] == "PENDING",
                msg=f"{node_id} was {state}",
            )

    def test_approve_resumes_run_to_success(self) -> None:
        """Approve follows the approve edge; the agent node replies."""
        run_id = self._start()
        self._poll(run_id, {"WAITING_HUMAN"})
        approved = self._client.post(
            f"/workflow/runs/{run_id}/approve",
            json={
                "node_id": "gate",
                "verdict": "approve",
                "note": "go on",
            },
            headers=HEADERS,
        )
        self.assertEqual(approved.status_code, 200)
        done = self._poll(run_id, {"SUCCEEDED"})
        self.assertEqual(done["context"]["gate"]["verdict"], "approve")
        self.assertEqual(done["context"]["gate"]["note"], "go on")
        self.assertEqual(done["node_states"]["ok"]["status"], "SUCCEEDED")
        self.assertEqual(done["node_states"]["no"]["status"], "CANCELLED")
        self.assertIn("FAKE-OK", done["context"]["ok"]["text"])

    def test_reject_takes_the_reject_branch(self) -> None:
        """Reject reaches the reject successor; the other is cancelled."""
        run_id = self._start()
        self._poll(run_id, {"WAITING_HUMAN"})
        rejected = self._approve(run_id, "gate", "reject")
        self.assertEqual(rejected.status_code, 200)
        done = self._poll(run_id, {"SUCCEEDED"})
        self.assertEqual(done["context"]["gate"]["verdict"], "reject")
        self.assertEqual(done["node_states"]["no"]["status"], "SUCCEEDED")
        self.assertEqual(done["node_states"]["ok"]["status"], "CANCELLED")

    def test_list_runs_newest_first_and_workflow_filter(self) -> None:
        """Runs list newest first; ``workflow_id`` narrows; users isolated."""
        first = self._start()
        self._poll(first, {"WAITING_HUMAN"})
        second = self._start()
        self._poll(second, {"WAITING_HUMAN"})

        listed = self._client.get("/workflow/runs", headers=HEADERS).json()
        self.assertEqual(listed["total"], 2)
        self.assertEqual(listed["runs"][0]["id"], second)
        self.assertEqual(listed["runs"][1]["id"], first)

        filtered = self._client.get(
            "/workflow/runs",
            params={"workflow_id": self._workflow_id},
            headers=HEADERS,
        ).json()
        self.assertEqual(filtered["total"], 2)

        missing = self._client.get(
            "/workflow/runs",
            params={"workflow_id": "wf-missing"},
            headers=HEADERS,
        ).json()
        self.assertEqual(missing["total"], 0)

        other = self._client.get(
            "/workflow/runs",
            headers={"X-User-ID": "bob"},
        ).json()
        self.assertEqual(other["total"], 0)

    def test_verdict_edge_cases(self) -> None:
        """Wrong node, unknown node, bad verdict and terminal runs."""
        run_id = self._start()
        self._poll(run_id, {"WAITING_HUMAN"})

        # A pending (not parked) node is not awaiting approval.
        self.assertEqual(
            self._approve(run_id, "ok", "approve").status_code,
            409,
        )
        # Neither is an unknown one.
        self.assertEqual(
            self._approve(run_id, "ghost", "approve").status_code,
            409,
        )
        # The verdict itself is constrained at the schema layer.
        bad = self._client.post(
            f"/workflow/runs/{run_id}/approve",
            json={"node_id": "gate", "verdict": "maybe", "note": ""},
            headers=HEADERS,
        )
        self.assertEqual(bad.status_code, 422)

        self.assertEqual(
            self._approve(run_id, "gate", "approve").status_code,
            200,
        )
        self._poll(run_id, {"SUCCEEDED"})
        # A finished run no longer accepts verdicts.
        self.assertEqual(
            self._approve(run_id, "gate", "approve").status_code,
            409,
        )

    def test_cancel_parked_run(self) -> None:
        """Cancel ends a parked run and its waiting node."""
        run_id = self._start()
        self._poll(run_id, {"WAITING_HUMAN"})
        cancelled = self._client.post(
            f"/workflow/runs/{run_id}/cancel",
            headers=HEADERS,
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["data"]["status"], "CANCELLED")
        done = self._poll(run_id, {"CANCELLED"})
        self.assertEqual(done["node_states"]["gate"]["status"], "CANCELLED")
        self.assertEqual(done["node_states"]["ok"]["status"], "CANCELLED")

    def test_retry_lifecycle(self) -> None:
        """Retry refuses non-failed runs; a failed one restarts."""
        run_id = self._start()
        self._poll(run_id, {"WAITING_HUMAN"})
        self.assertEqual(
            self._client.post(
                f"/workflow/runs/{run_id}/retry",
                headers=HEADERS,
            ).status_code,
            409,
        )

        # A scripted-failing model fails the agent node (and so the
        # run) once the gate is approved.
        failing = self._start(model="fail-model")
        self._poll(failing, {"WAITING_HUMAN"})
        self.assertEqual(
            self._approve(failing, "gate", "approve").status_code,
            200,
        )
        failed = self._poll(failing, {"FAILED"})
        self.assertEqual(failed["node_states"]["ok"]["status"], "FAILED")

        retried = self._client.post(
            f"/workflow/runs/{failing}/retry",
            headers=HEADERS,
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["data"]["status"], "PENDING")
        # The fresh dispatch fails through the same scripted model.
        again = self._poll(failing, {"FAILED"})
        self.assertEqual(again["node_states"]["ok"]["status"], "FAILED")

    def test_unknown_run_or_workflow_is_404(self) -> None:
        """Run reads and transitions answer 404 on missing ids."""
        self.assertEqual(
            self._client.get(
                "/workflow/runs/run-missing",
                headers=HEADERS,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                "/workflow/runs/run-missing/cancel",
                headers=HEADERS,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                "/workflow/runs/run-missing/retry",
                headers=HEADERS,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                "/workflow/runs/run-missing/approve",
                json={"node_id": "gate", "verdict": "approve", "note": ""},
                headers=HEADERS,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                "/workflow/wf-missing/run",
                json={"input": {}},
                headers=HEADERS,
            ).status_code,
            404,
        )

    def test_start_run_without_model_is_422(self) -> None:
        """Modelless agent nodes without a run default are rejected."""
        started = self._client.post(
            f"/workflow/{self._workflow_id}/run",
            json={"input": {}},
            headers=HEADERS,
        )
        self.assertEqual(started.status_code, 422)
        problems = started.json()["detail"]["problems"]
        self.assertTrue(
            any("no model" in problem for problem in problems),
            msg=f"problems were {problems}",
        )

    def test_openapi_lists_run_routes(self) -> None:
        """The run routes exist and never collide with ``/{workflow_id}``."""
        paths = self._client.get("/openapi.json").json()["paths"]
        self.assertIn("/workflow/runs", paths)
        self.assertIn("get", paths["/workflow/runs"])
        self.assertIn("/workflow/runs/{run_id}", paths)
        self.assertIn("get", paths["/workflow/runs/{run_id}"])
        self.assertIn("/workflow/runs/{run_id}/approve", paths)
        self.assertIn("/workflow/runs/{run_id}/cancel", paths)
        self.assertIn("/workflow/runs/{run_id}/retry", paths)
        self.assertIn("/workflow/{workflow_id}/run", paths)
        self.assertIn("post", paths["/workflow/{workflow_id}/run"])
        # The CRUD route stays reachable in its own right.
        self.assertIn("/workflow/{workflow_id}", paths)
        self.assertIn("get", paths["/workflow/{workflow_id}"])
