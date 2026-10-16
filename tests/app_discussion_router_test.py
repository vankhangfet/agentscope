# -*- coding: utf-8 -*-
"""Discussion REST endpoints over a real app and a fake model.

The chat path is faked at the credential layer (the run-router test's
pattern): a registered credential whose model class replies instantly,
deriving each reply from what the prompt asks for — the strict verdict
JSON when the judge is asked to verdict, conclusion text when the
debate ends, an argument otherwise — with the model NAME steering the
verdict (``judge-consensus`` flags consensus, ``judge-broken`` replies
prose). ``POST /discussion`` therefore exercises the whole stack —
router, facade validation, engine, chat service, storage — with no
network.
"""
import json
import re
import tempfile
import time
from typing import Any, AsyncGenerator, Literal, Type
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import (
    ChatModelConfig,
    DebaterConfig,
    DiscussionData,
    DiscussionRecord,
    DiscussionStatus,
    JudgeConfig,
    RedisStorage,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import ToolChoice

HEADERS = {"X-User-ID": "alice"}

_VERDICT_ASK = "Reply with ONE JSON object"
_CONCLUSION_ASK = "final conclusion"


def _judge_json(
    names: tuple[str, ...],
    *,
    consensus: bool,
) -> str:
    """A strict-JSON verdict over *names*, alternating strengths."""
    assessments = [
        {
            "debater": name,
            "strength": 4 if index % 2 == 0 else 3,
            "rebutted": index % 2 == 1,
            "note": f"note {index}",
        }
        for index, name in enumerate(names)
    ]
    return json.dumps(
        {
            "assessments": assessments,
            "consensus": consensus,
            "summary": "the round in one line",
        },
    )


class _FakeDiscussionCredential(CredentialBase):
    """A credential whose chat model answers instantly, no network."""

    type: Literal["fake_discussion_credential"] = "fake_discussion_credential"
    """The credential type discriminator."""

    @classmethod
    def get_chat_model_class(cls) -> Type[ChatModelBase]:
        """Return the fake model class this credential resolves to."""
        return _FakeDiscussionModel


class _FakeDiscussionModel(ChatModelBase):
    """A model that answers as the debate needs, steered by its name.

    The reply is derived from the ask, the way the engine's prompts
    structure one: a verdict ask (``_VERDICT_ASK``) is answered with
    the strict verdict JSON — assessing the debater names the prompt
    lists, ``consensus`` true only for a model named
    ``judge-consensus`` — a conclusion ask with conclusion text, and
    anything else with an argument naming the model. A model named
    ``judge-broken`` answers prose to every verdict ask, so its judge
    degrades and the rounds go unevaluated.
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
        """Answer as the debate needs: verdict, conclusion, argument."""
        del tools, tool_choice, kwargs
        prompt = ""
        for message in reversed(messages):
            prompt = message.get_text_content() or ""
            if prompt:
                break
        if _VERDICT_ASK in prompt:
            text = (
                "I refuse to answer in JSON."
                if model_name.startswith("judge-broken")
                else _judge_json(
                    names=tuple(
                        re.findall(r"^\[(.+?)\] ", prompt, re.MULTILINE),
                    ),
                    consensus=model_name.startswith("judge-consensus"),
                )
            )
        elif _CONCLUSION_ASK in prompt:
            text = "CONCLUSION: the sides met in the middle."
        else:
            text = f"FAKE-ARG:{model_name}"
        return ChatResponse(
            content=[TextBlock(text=text)],
            is_last=True,
        )


CredentialFactory.register_credential(_FakeDiscussionCredential)


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


def _seeded_record(status: DiscussionStatus) -> DiscussionRecord:
    """A two-debater discussion planted straight into storage.

    Seeding bypasses the engine so a test observes exactly the status
    it asked for, without racing real transitions. The model configs
    are placeholders: nothing ever drives a seeded record's turns.
    """
    model = ChatModelConfig(
        type="fake",
        credential_id="c-unused",
        model="m",
        parameters={},
    )
    return DiscussionRecord(
        user_id="alice",
        data=DiscussionData(
            topic="seeded",
            max_rounds=1,
            debaters=[
                DebaterConfig(
                    name="pro",
                    system_prompt="pro",
                    chat_model_config=model,
                ),
                DebaterConfig(
                    name="con",
                    system_prompt="con",
                    chat_model_config=model,
                ),
            ],
            judge=JudgeConfig(chat_model_config=model),
            status=status,
        ),
    )


class DiscussionRouterTest(IsolatedAsyncioTestCase):
    """Create, list, read and cancel discussions over HTTP."""

    def setUp(self) -> None:
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        self._storage = storage
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
            json={
                "data": {
                    "type": "fake_discussion_credential",
                    "name": "fake",
                },
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        self._credential_id = created.json()["credential_id"]

    # ------------------------------------------------------------- helpers

    def _model(self, name: str) -> dict:
        """A model config resolving to the fake credential."""
        return {
            "type": "fake",
            "credential_id": self._credential_id,
            "model": name,
            "parameters": {},
        }

    def _debater(self, name: str, model: str) -> dict:
        """One debater's config, arguing through *model*."""
        return {
            "name": name,
            "system_prompt": f"You argue for {name}.",
            "chat_model_config": self._model(model),
        }

    def _body(
        self,
        *,
        judge_model: str | None = "judge-consensus",
        max_rounds: int = 1,
        topic: str = "cats vs dogs",
        debaters: list[dict] | None = None,
    ) -> dict:
        """A create-discussion body, two debaters by default."""
        if debaters is None:
            debaters = [
                self._debater("pro", "debater-pro"),
                self._debater("con", "debater-con"),
            ]
        return {
            "topic": topic,
            "debaters": debaters,
            "judge": None
            if judge_model is None
            else {"chat_model_config": self._model(judge_model)},
            "max_rounds": max_rounds,
        }

    def _start(self, **overrides: Any) -> str:
        """Create a discussion over HTTP and return its id."""
        created = self._client.post(
            "/discussion/",
            json=self._body(**overrides),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["discussion_id"]

    def _data(self, discussion_id: str) -> dict:
        """One discussion's persisted content."""
        fetched = self._client.get(
            f"/discussion/{discussion_id}",
            headers=HEADERS,
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        return fetched.json()["data"]

    def _poll(
        self,
        discussion_id: str,
        statuses: set[str],
        timeout: float = 10.0,
    ) -> dict:
        """Poll a discussion until it reaches one of *statuses*."""
        deadline = time.monotonic() + timeout
        data: dict | None = None
        while time.monotonic() < deadline:
            data = self._data(discussion_id)
            if data["status"] in statuses:
                return data
            time.sleep(0.05)
        self.fail(
            f"Discussion {discussion_id!r} never reached {statuses}; "
            f"last status {data and data['status']!r}.",
        )

    # --------------------------------------------------------------- tests

    def test_create_completes_consensus_round(self) -> None:
        """A created debate argues, is judged, and completes."""
        discussion_id = self._start()
        # Freshly created: PENDING, or already RUNNING once the loop
        # task takes over on the app's event loop.
        self.assertIn(
            self._data(discussion_id)["status"],
            {"PENDING", "RUNNING"},
        )

        done = self._poll(discussion_id, {"COMPLETED"})
        self.assertIsNone(done["error"])
        self.assertIsNotNone(done["finished_at"])
        self.assertEqual(done["trigger"], "api")
        self.assertEqual(len(done["rounds"]), 1)
        round_one = done["rounds"][0]
        self.assertTrue(round_one["consensus"])
        self.assertEqual(round_one["summary"], "the round in one line")
        self.assertEqual(
            [argument["debater"] for argument in round_one["arguments"]],
            ["pro", "con"],
        )
        self.assertEqual(
            [
                argument["text"]
                for argument in round_one["arguments"]
            ],
            ["FAKE-ARG:debater-pro", "FAKE-ARG:debater-con"],
        )
        self.assertEqual(
            [
                argument["strength"]
                for argument in round_one["arguments"]
            ],
            [4, 3],
        )
        self.assertEqual(
            [argument["rebutted"] for argument in round_one["arguments"]],
            [False, True],
        )
        self.assertTrue(done["conclusion"])
        self.assertIn("CONCLUSION", done["conclusion"])

    def test_debate_runs_to_max_rounds(self) -> None:
        """Without consensus the debate argues every round it may."""
        discussion_id = self._start(
            judge_model="judge-strict",
            max_rounds=2,
        )
        done = self._poll(discussion_id, {"COMPLETED"})
        self.assertEqual(len(done["rounds"]), 2)
        for evaluation in done["rounds"]:
            self.assertFalse(evaluation["consensus"])
        self.assertTrue(done["conclusion"])

    def test_default_judge_runs_on_first_debaters_model(self) -> None:
        """No judge given: the evaluator rides the first debater's model."""
        discussion_id = self._start(judge_model=None)
        done = self._poll(discussion_id, {"COMPLETED"})
        self.assertEqual(
            done["judge"]["chat_model_config"]["model"],
            "debater-pro",
        )
        arguments = done["rounds"][0]["arguments"]
        self.assertEqual(
            [argument["strength"] for argument in arguments],
            [4, 3],
        )

    def test_judge_broken_degrades_round_but_completes(self) -> None:
        """A judge replying prose degrades the round, not the debate."""
        discussion_id = self._start(judge_model="judge-broken")
        done = self._poll(discussion_id, {"COMPLETED"})
        evaluation = done["rounds"][0]
        for argument in evaluation["arguments"]:
            self.assertIsNone(argument["strength"])
            self.assertIsNone(argument["rebutted"])
            self.assertEqual(argument["note"], "")
        self.assertFalse(evaluation["consensus"])
        self.assertIn("judge unavailable", evaluation["summary"])
        # The conclusion still ran — the debate survives its judge.
        self.assertTrue(done["conclusion"])

    def test_list_newest_first_and_user_isolation(self) -> None:
        """Discussions list newest first; users only see their own."""
        first = self._start()
        self._poll(first, {"COMPLETED"})
        second = self._start()
        self._poll(second, {"COMPLETED"})

        listed = self._client.get("/discussion/", headers=HEADERS).json()
        self.assertEqual(listed["total"], 2)
        self.assertEqual(listed["discussions"][0]["id"], second)
        self.assertEqual(listed["discussions"][1]["id"], first)

        other = self._client.get(
            "/discussion/",
            headers={"X-User-ID": "bob"},
        ).json()
        self.assertEqual(other["total"], 0)

    async def test_list_status_filter(self) -> None:
        """``?status=`` narrows the listing; unknown values answer 422."""
        running = await self._storage.upsert_discussion(
            "alice",
            _seeded_record(DiscussionStatus.RUNNING),
        )
        completed = await self._storage.upsert_discussion(
            "alice",
            _seeded_record(DiscussionStatus.COMPLETED),
        )

        filtered = self._client.get(
            "/discussion/",
            params={"status": "RUNNING"},
            headers=HEADERS,
        )
        self.assertEqual(filtered.status_code, 200)
        body = filtered.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["discussions"][0]["id"], running.id)

        done = self._client.get(
            "/discussion/",
            params={"status": "COMPLETED"},
            headers=HEADERS,
        ).json()
        self.assertEqual(done["total"], 1)
        self.assertEqual(done["discussions"][0]["id"], completed.id)

        bogus = self._client.get(
            "/discussion/",
            params={"status": "BOGUS"},
            headers=HEADERS,
        )
        self.assertEqual(bogus.status_code, 422)
        detail = bogus.json()["detail"]
        self.assertIn("BOGUS", detail)
        # The 422 teaches the allowed vocabulary.
        self.assertIn("PENDING", detail)
        self.assertIn("RUNNING", detail)
        self.assertIn("CANCELLED", detail)

    async def test_cancel_pending_discussion(self) -> None:
        """Cancelling a never-dispatched discussion ends it CANCELLED."""
        seeded = await self._storage.upsert_discussion(
            "alice",
            _seeded_record(DiscussionStatus.PENDING),
        )
        cancelled = self._client.post(
            f"/discussion/{seeded.id}/cancel",
            headers=HEADERS,
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["data"]["status"], "CANCELLED")
        self.assertEqual(cancelled.json()["data"]["rounds"], [])

    def test_cancel_live_discussion_ends_terminal(self) -> None:
        """Cancelling a just-created debate lands a terminal state.

        The cancel races the loop task on the app's event loop: it
        either stops the debate mid-flight (CANCELLED) or arrives after
        the single fast round finished it (COMPLETED). Both are honest
        answers; the endpoint must never leave it non-terminal.
        """
        discussion_id = self._start()
        cancelled = self._client.post(
            f"/discussion/{discussion_id}/cancel",
            headers=HEADERS,
        )
        self.assertEqual(cancelled.status_code, 200)
        final = self._poll(discussion_id, {"CANCELLED", "COMPLETED"})
        self.assertIn(final["status"], {"CANCELLED", "COMPLETED"})

    def test_unknown_discussion_is_404(self) -> None:
        """Reads and cancels answer 404 on missing ids."""
        self.assertEqual(
            self._client.get(
                "/discussion/missing",
                headers=HEADERS,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                "/discussion/missing/cancel",
                headers=HEADERS,
            ).status_code,
            404,
        )

    def test_create_validation_rejections(self) -> None:
        """Structural bounds answer 422 before anything is dispatched."""
        seven = [self._debater(f"d{i}", "m") for i in range(7)]
        cases = (
            (self._body(debaters=seven), "7 debaters"),
            (self._body(debaters=[]), "no debaters"),
            (self._body(max_rounds=11), "max_rounds 11"),
            (self._body(max_rounds=0), "max_rounds 0"),
            (self._body(topic=""), "empty topic"),
            (self._body(topic="   "), "whitespace topic"),
            (
                self._body(
                    debaters=[
                        self._debater("pro", "m"),
                        self._debater("pro", "m"),
                    ],
                ),
                "duplicate names",
            ),
        )
        for body, why in cases:
            with self.subTest(why):
                created = self._client.post(
                    "/discussion/",
                    json=body,
                    headers=HEADERS,
                )
                self.assertEqual(created.status_code, 422, created.text)
        # Nothing above persisted anything.
        listed = self._client.get("/discussion/", headers=HEADERS).json()
        self.assertEqual(listed["total"], 0)

    def test_openapi_lists_discussion_routes(self) -> None:
        """The discussion routes exist and never collide."""
        paths = self._client.get("/openapi.json").json()["paths"]
        self.assertIn("/discussion/", paths)
        self.assertIn("post", paths["/discussion/"])
        self.assertIn("get", paths["/discussion/"])
        self.assertIn("/discussion/{discussion_id}", paths)
        self.assertIn("get", paths["/discussion/{discussion_id}"])
        self.assertIn("/discussion/{discussion_id}/cancel", paths)
        self.assertIn("post", paths["/discussion/{discussion_id}/cancel"])
