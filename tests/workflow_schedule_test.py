# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Workflow-target schedules: model fields, fire branch, router, E2E.

A schedule whose ``ScheduleData.workflow_id`` is set fires a workflow
run (``trigger == "schedule"``) instead of delivering a prompt to an
agent session. Covered bottom-up:

- model — the new fields round-trip and default to ``None`` / ``{}``;
- manager — ``_trigger`` hands workflow fires to the injected
  ``workflow_starter``, and never lets a failure kill the job;
- router — create / update accept the fields and reject unknown
  workflows with 404;
- E2E — a fired schedule starts a run that succeeds under a fake
  model, with no run-level model passed.
"""
import asyncio
import tempfile
import time
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, AsyncGenerator, Literal, Type
from unittest import IsolatedAsyncioTestCase, TestCase

import fakeredis.aioredis
import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel

from utils import FakeWorkspaceManager

from agentscope.app import create_app
from agentscope.app._lifespan import lifespan
from agentscope.app._manager import SchedulerManager
from agentscope.app.message_bus import MessageBusKeys, RedisMessageBus
from agentscope.app.storage import (
    ChatModelConfig,
    RedisStorage,
    ScheduleData,
    ScheduleRecord,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import ToolChoice

HEADERS = {"X-User-ID": "alice"}


# ------------------------------------------------------------- fixtures

def _fake_backends() -> tuple[RedisStorage, RedisMessageBus]:
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


class _FakeScheduleCredential(CredentialBase):
    """A credential whose chat model answers instantly, no network."""

    type: Literal["fake_schedule_credential"] = "fake_schedule_credential"
    """The credential type discriminator."""

    @classmethod
    def get_chat_model_class(cls) -> Type[ChatModelBase]:
        """Return the fake model class this credential resolves to."""
        return _FakeScheduleModel


class _FakeScheduleModel(ChatModelBase):
    """A model that replies with one text block."""

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
        """Answer with one text block naming the model it ran as."""
        del messages, tools, tool_choice, kwargs
        return ChatResponse(
            content=[TextBlock(text=f"FAKE-OK:{model_name}")],
            is_last=True,
        )


CredentialFactory.register_credential(_FakeScheduleCredential)


def _graph(agent_id: str) -> dict:
    """A single-agent-node workflow: runs straight to SUCCEEDED."""
    return {
        "nodes": [
            {"id": "ok", "config": {"type": "agent", "agent_id": agent_id}},
        ],
        "edges": [],
    }


def _model_config() -> ChatModelConfig:
    """A minimal (never resolved) schedule-level model config."""
    return ChatModelConfig(
        type="dashscope_credential",
        credential_id="c",
        model="m",
        parameters={},
    )


# ---------------------------------------------------------------- model

class ScheduleDataWorkflowFieldTest(TestCase):
    """The new ``ScheduleData`` fields default and round-trip."""

    def _data(self, **extra: Any) -> ScheduleData:
        """A minimal schedule payload plus *extra* field overrides."""
        return ScheduleData(
            name="sched",
            cron_expression="0 0 * * *",
            started_at=datetime(2025, 1, 1),
            chat_model_config=_model_config(),
            **extra,
        )

    def test_defaults(self) -> None:
        """Without the new fields a schedule still targets an agent."""
        data = self._data()

        self.assertIsNone(data.workflow_id)
        self.assertDictEqual(data.run_input, {})

    def test_round_trip(self) -> None:
        """The fields survive a JSON dump / parse cycle."""
        data = self._data(
            workflow_id="wf-1",
            run_input={"topic": "reports", "count": 3},
        )

        restored = ScheduleData.model_validate(data.model_dump(mode="json"))

        self.assertEqual(restored.workflow_id, "wf-1")
        self.assertDictEqual(
            restored.run_input,
            {"topic": "reports", "count": 3},
        )


# -------------------------------------------------------------- manager

def _record(*, workflow_id: str | None = "wf-1") -> ScheduleRecord:
    """A schedule record, by default targeting a workflow."""
    return ScheduleRecord(
        user_id="u",
        agent_id="a",
        data=ScheduleData(
            name="sched-wf",
            cron_expression="0 0 * * *",
            started_at=datetime(2025, 1, 1),
            chat_model_config=_model_config(),
            workflow_id=workflow_id,
            run_input={"topic": "reports"} if workflow_id else {},
        ),
    )


class _SchedulerWorkflowTestBase(IsolatedAsyncioTestCase):
    """fakeredis-backed storage + bus; the manager stays un-started."""

    async def asyncSetUp(self) -> None:
        self._stack = AsyncExitStack()
        self.storage, self.bus = _fake_backends()
        await self._stack.enter_async_context(self.storage)
        await self._stack.enter_async_context(self.bus)

    async def asyncTearDown(self) -> None:
        await self._stack.aclose()

    def _manager(
        self,
        starter: Any = None,
    ) -> SchedulerManager:
        """A manager wired with *starter* (``None`` leaves it unset)."""
        return SchedulerManager(
            storage=self.storage,
            message_bus=self.bus,
            workspace_manager=FakeWorkspaceManager(),
            workflow_starter=starter,
        )


class SchedulerWorkflowFireTest(_SchedulerWorkflowTestBase):
    """The workflow branch of the trigger closure."""

    async def test_fire_calls_the_starter_once(self) -> None:
        """A workflow fire reaches only the starter — no session, no
        wakeup is created for the record's agent."""
        calls: list[tuple[str, ScheduleRecord]] = []

        async def starter(
            user_id: str,
            record: ScheduleRecord,
        ) -> None:
            calls.append((user_id, record))

        record = _record()

        await self._manager(starter)._build_trigger(record)()

        self.assertListEqual(calls, [("u", record)])
        self.assertListEqual(
            await self.storage.list_sessions("u", "a"),
            [],
        )
        self.assertListEqual(
            await self.bus.queue_drain(
                MessageBusKeys.wakeup_queue(),
                max_count=10,
            ),
            [],
        )

    async def test_fire_without_a_starter_skips(self) -> None:
        """No starter wired (older wiring): warn and skip, no raise."""
        await self._manager()._build_trigger(_record())()

        self.assertListEqual(
            await self.storage.list_sessions("u", "a"),
            [],
        )

    async def test_starter_failure_does_not_escape(self) -> None:
        """A raising starter is logged inside the trigger, never
        propagated to (and so never able to kill) the APScheduler job."""

        async def boom(user_id: str, record: ScheduleRecord) -> None:
            raise RuntimeError("start_run exploded")

        await self._manager(boom)._build_trigger(_record())()

    async def test_agent_target_keeps_the_agent_path(self) -> None:
        """A record without ``workflow_id`` still creates a session,
        starter or not."""
        await self._manager()._build_trigger(_record(workflow_id=None))()

        sessions = await self.storage.list_sessions("u", "a")
        self.assertEqual(len(sessions), 1)


# --------------------------------------------------------------- router

class ScheduleRouterWorkflowTest(TestCase):
    """Create / update of workflow-target schedules over HTTP."""

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
        self._agent_id = self._client.post(
            "/agent/",
            json={"name": "worker", "system_prompt": "hi"},
            headers=HEADERS,
        ).json()["agent_id"]
        created = self._client.post(
            "/workflow/",
            json={
                "data": {
                    "name": "daily",
                    "graph": _graph(self._agent_id),
                },
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        self._workflow_id = created.json()["workflow_id"]

    def _schedule_body(self, **overrides: Any) -> dict:
        """A create-schedule body targeting the fixture workflow."""
        body = {
            "name": "nightly",
            "cron_expression": "0 3 * * *",
            "timezone": "UTC",
            # Placeholders on purpose: a workflow target resolves
            # neither the agent nor the credential.
            "agent_id": "not-an-agent",
            "chat_model_config": {
                "type": "fake",
                "credential_id": "not-a-credential",
                "model": "m",
                "parameters": {},
            },
            "workflow_id": self._workflow_id,
            "run_input": {"topic": "reports"},
        }
        body.update(overrides)
        return body

    def _stored(self) -> list[dict]:
        """The caller's persisted schedules, as JSON."""
        listed = self._client.get("/schedule/", headers=HEADERS)
        self.assertEqual(listed.status_code, 200)
        return listed.json()["schedules"]

    def _create(self, **overrides: Any) -> str:
        """Create a schedule from a (possibly overridden) body."""
        created = self._client.post(
            "/schedule/",
            json=self._schedule_body(**overrides),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        return created.json()["schedule_id"]

    def test_create_round_trips_the_workflow_fields(self) -> None:
        """A workflow-target schedule persists its new fields."""
        self._create()

        schedules = self._stored()
        self.assertEqual(len(schedules), 1)
        data = schedules[0]["data"]
        self.assertEqual(data["workflow_id"], self._workflow_id)
        self.assertDictEqual(data["run_input"], {"topic": "reports"})
        self.assertTrue(data["enabled"])

    def test_create_with_unknown_workflow_is_404(self) -> None:
        """An unknown workflow id is rejected before anything is
        stored."""
        created = self._client.post(
            "/schedule/",
            json=self._schedule_body(workflow_id="wf-missing"),
            headers=HEADERS,
        )

        self.assertEqual(created.status_code, 404)
        self.assertIn("wf-missing", created.json()["detail"])
        self.assertListEqual(self._stored(), [])

    def test_update_toggles_enabled(self) -> None:
        """Disabling a workflow-target schedule persists the flag."""
        schedule_id = self._create()

        patched = self._client.patch(
            f"/schedule/{schedule_id}",
            json={"enabled": False},
            headers=HEADERS,
        )

        self.assertEqual(patched.status_code, 200)
        self.assertFalse(patched.json()["data"]["enabled"])
        self.assertFalse(self._stored()[0]["data"]["enabled"])

    def test_update_with_unknown_workflow_is_404(self) -> None:
        """Retargeting to an unknown workflow changes nothing."""
        schedule_id = self._create()

        patched = self._client.patch(
            f"/schedule/{schedule_id}",
            json={"workflow_id": "wf-missing"},
            headers=HEADERS,
        )

        self.assertEqual(patched.status_code, 404)
        self.assertIn("wf-missing", patched.json()["detail"])
        self.assertEqual(
            self._stored()[0]["data"]["workflow_id"],
            self._workflow_id,
        )

    def test_patch_after_workflow_deletion_still_disables(self) -> None:
        """Updates re-validate only actual retargets: once the workflow
        is gone, the enabled flag remains the schedule's escape hatch."""
        schedule_id = self._create()

        deleted = self._client.delete(
            f"/workflow/{self._workflow_id}",
            headers=HEADERS,
        )
        self.assertEqual(deleted.status_code, 204)

        patched = self._client.patch(
            f"/schedule/{schedule_id}",
            json={"enabled": False},
            headers=HEADERS,
        )

        self.assertEqual(patched.status_code, 200)
        self.assertFalse(patched.json()["data"]["enabled"])
        self.assertFalse(self._stored()[0]["data"]["enabled"])

    def test_update_with_empty_workflow_id_is_treated_as_omitted(
        self,
    ) -> None:
        """An empty ``workflow_id`` neither retargets nor 404s — it
        keeps the current target, mirroring create's normalization."""
        schedule_id = self._create()

        patched = self._client.patch(
            f"/schedule/{schedule_id}",
            json={"workflow_id": ""},
            headers=HEADERS,
        )

        self.assertEqual(patched.status_code, 200)
        self.assertEqual(
            patched.json()["data"]["workflow_id"],
            self._workflow_id,
        )
        self.assertEqual(
            self._stored()[0]["data"]["workflow_id"],
            self._workflow_id,
        )


# ------------------------------------------------------------------ e2e

class ScheduledWorkflowRunE2ETest(IsolatedAsyncioTestCase):
    """A fired workflow-target schedule runs the workflow end to end.

    The app — workflow engine, chat service, scheduler — runs on the
    test's own loop via ``ASGITransport``, so the manual trigger fire
    and the engine's dispatch task interleave with the polling
    requests exactly as they would under a live server.
    """

    async def test_fired_schedule_runs_the_workflow(self) -> None:
        """Fire once; one run appears, started by the schedule, and
        succeeds under the fake model."""
        with tempfile.TemporaryDirectory() as workdir:
            storage, bus = _fake_backends()
            app = create_app(
                storage=storage,
                message_bus=bus,
                workspace_manager=LocalWorkspaceManager(workdir),
                enable_index_worker=False,
                # Timers stay off: the test fires the trigger directly.
                enable_scheduler=False,
            )
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(lifespan(app))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                    headers=HEADERS,
                    timeout=10.0,
                ) as client:
                    await self._scenario(client, app)

    async def _scenario(self, client: httpx.AsyncClient, app: Any) -> None:
        """Seed, schedule, fire, and assert on the resulting run."""
        credential = await client.post(
            "/credential/",
            json={
                "data": {
                    "type": "fake_schedule_credential",
                    "name": "fake",
                },
            },
        )
        self.assertEqual(credential.status_code, 201)
        credential_id = credential.json()["credential_id"]

        # The agent carries the model on its template: scheduled runs
        # pass no run-level default, so resolution must come from here.
        agent = await client.post(
            "/agent/",
            json={
                "name": "worker",
                "system_prompt": "hi",
                "chat_model_config": {
                    "type": "fake",
                    "credential_id": credential_id,
                    "model": "ok-model",
                    "parameters": {},
                },
            },
        )
        self.assertEqual(agent.status_code, 201)
        agent_id = agent.json()["agent_id"]

        workflow = await client.post(
            "/workflow/",
            json={"data": {"name": "daily", "graph": _graph(agent_id)}},
        )
        self.assertEqual(workflow.status_code, 201)
        workflow_id = workflow.json()["workflow_id"]

        created = await client.post(
            "/schedule/",
            json={
                "name": "nightly",
                "cron_expression": "0 3 * * *",
                "agent_id": "not-an-agent",
                "chat_model_config": {
                    "type": "fake",
                    "credential_id": "not-a-credential",
                    "model": "m",
                    "parameters": {},
                },
                "workflow_id": workflow_id,
                "run_input": {"topic": "reports"},
            },
        )
        self.assertEqual(created.status_code, 201)

        listed = (await client.get("/schedule/")).json()["schedules"]
        record = ScheduleRecord.model_validate(listed[0])

        # Fire the way APScheduler would.
        await app.state.scheduler_manager._build_trigger(record)()

        run = await self._poll_run(client, workflow_id)
        self.assertEqual(run["status"], "SUCCEEDED")
        self.assertEqual(run["trigger"], "schedule")
        self.assertDictEqual(run["input"], {"topic": "reports"})
        self.assertIn("FAKE-OK", run["context"]["ok"]["text"])

    async def _poll_run(
        self,
        client: httpx.AsyncClient,
        workflow_id: str,
    ) -> dict:
        """Poll the workflow's runs until exactly one finishes."""
        deadline = time.monotonic() + 10.0
        data: dict | None = None
        while time.monotonic() < deadline:
            listed = (
                await client.get(
                    "/workflow/runs",
                    params={"workflow_id": workflow_id},
                )
            ).json()
            if listed["total"] == 1:
                data = listed["runs"][0]["data"]
                if data["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    return data
            await asyncio.sleep(0.05)
        self.fail(f"Scheduled run never finished; last data {data!r}.")
