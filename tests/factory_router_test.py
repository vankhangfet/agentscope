# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Factory REST endpoints and lifecycle wiring over a real app.

The chat path is faked at the credential layer (the discussion
router test's pattern): a registered credential whose model class
answers instantly, deriving each reply from what the prompt asks for
— the strict verdict JSON when a judge is asked to verdict, plan JSON
when the Plan Lead is asked to plan, a report when a developer is
asked to work, a review when the reviewer is asked — so ``POST
/factory`` exercises the whole stack (router, engine, debate engine,
chat service, storage) with no network. The verdict always reports
consensus, so each phase discussion settles in one round; a
class-level ``blocking`` knob parks the model mid-call, which is how
the shutdown test holds a walker inside its first debate.

The lifespan class boots its own apps: seeded straight into storage
before startup, a RUNNING ticket must be failed out as ``interrupted
by restart`` by the factory engine's boot sweep, and a walker caught
mid-turn at shutdown must be cancelled with its record left honestly
mid-flight.
"""
import asyncio
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
from agentscope.app._router._factory import factory_router
from agentscope.app._router._schema import FactoryTicketView
from agentscope.app._service.factory import FactoryEngine
from agentscope.app._service.factory._roles import (
    ARCHITECTURE_ROLES,
    REQUIREMENTS_ROLES,
    to_debaters,
)
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import (
    ChatModelConfig,
    FactoryPhase,
    FactoryStatus,
    FactoryTicketData,
    FactoryTicketRecord,
    RedisStorage,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import ToolChoice

from app_discussion_router_test import (
    _CONCLUSION_ASK,
    _VERDICT_ASK,
    _fake_backends,
    _judge_json,
)

HEADERS = {"X-User-ID": "alice"}

_PLAN_ASK = "strict JSON object"
"""Marker of the plan prompt: the ask the Plan Lead replies to."""

_TASK_ASK = "Task "
"""A developer prompt opens with its task header."""

_REVIEW_ASK = "Review the development"
"""Marker of the reviewer prompt."""

_GOOD_PLAN = {
    "tasks": [
        {"title": "write the module", "description": "first task"},
        {"title": "test the module", "description": "second task"},
    ],
}
"""The plan the fake Plan Lead files when nothing overrides it."""

_CONCLUSION_TEXT = "CONCLUSION: the artifact the phase needed."
"""What every phase discussion concludes with."""


class _FakeFactoryCredential(CredentialBase):
    """A credential whose chat model answers instantly, no network."""

    type: Literal["fake_factory_credential"] = "fake_factory_credential"
    """The credential type discriminator."""

    @classmethod
    def get_chat_model_class(cls) -> Type[ChatModelBase]:
        """Return the fake model class this credential resolves to."""
        return _FakeFactoryModel


class _FakeFactoryModel(ChatModelBase):
    """A model that answers as every factory role needs, steered by
    the ask: the debate turns keep the discussion fake's behaviour
    (a verdict JSON whose consensus is always true — a phase
    discussion settles in one round — conclusion text, an argument
    otherwise), and the three DEVELOP roles are recognized from what
    their prompt asks for: plan JSON for the Plan Lead, a report for
    a developer, a review for the reviewer. ``blocking`` parks every
    call, the knob the shutdown test uses to hold a walker mid-turn.
    """

    blocking = False
    """Class-level: when True every call sleeps instead of answering."""

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
        """Answer as the role being asked: verdict, plan, task,
        review, conclusion or argument."""
        del tools, tool_choice, kwargs
        if _FakeFactoryModel.blocking:
            await asyncio.sleep(30)
        prompt = ""
        for message in reversed(messages):
            prompt = message.get_text_content() or ""
            if prompt:
                break
        if _VERDICT_ASK in prompt:
            text = _judge_json(
                names=tuple(
                    re.findall(r"^\[(.+?)\] ", prompt, re.MULTILINE),
                ),
                consensus=True,
            )
        elif _PLAN_ASK in prompt:
            text = json.dumps(_GOOD_PLAN)
        elif prompt.startswith(_TASK_ASK):
            header = prompt.split("\n", 1)[0]
            text = f"done: {header.split(': ', 1)[1]}"
        elif _REVIEW_ASK in prompt:
            text = "APPROVE: the work is sound and verified."
        elif _CONCLUSION_ASK in prompt:
            text = _CONCLUSION_TEXT
        else:
            text = f"FAKE-ARG:{model_name}"
        return ChatResponse(
            content=[TextBlock(text=text)],
            is_last=True,
        )


CredentialFactory.register_credential(_FakeFactoryCredential)


def _seeded_ticket(
    status: FactoryStatus,
    *,
    phase: FactoryPhase = FactoryPhase.REQUIREMENTS,
) -> FactoryTicketRecord:
    """A ticket planted straight into storage, never dispatched.

    Seeding bypasses the engine so a test observes exactly the status
    it asked for, without racing real transitions. The model config is
    a placeholder: nothing ever drives a seeded record's turns.
    """
    return FactoryTicketRecord(
        user_id="alice",
        data=FactoryTicketData(
            title="seeded",
            description="planted, never driven",
            status=status,
            phase=phase,
            chat_model_config=ChatModelConfig(
                type="fake",
                credential_id="c-unused",
                model="m",
                parameters={},
            ),
        ),
    )


def _backends() -> tuple:
    """Fakeredis backends whose redis handle the caller keeps."""
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

    return redis, _Storage(), _Bus()


def _reader(redis: Any) -> RedisStorage:
    """A storage handle on *redis*, usable outside the app's loop."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = redis
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


class FactoryRouterTest(IsolatedAsyncioTestCase):
    """Open, list, read and answer factory tickets over HTTP."""

    def setUp(self) -> None:
        _FakeFactoryModel.blocking = False
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
                    "type": "fake_factory_credential",
                    "name": "fake",
                },
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        self._credential_id = created.json()["credential_id"]

    # ------------------------------------------------------------- helpers

    def _model(self, name: str = "factory-model") -> dict:
        """A model config resolving to the fake credential."""
        return {
            "type": "fake",
            "credential_id": self._credential_id,
            "model": name,
            "parameters": {},
        }

    def _body(
        self,
        *,
        title: str = "ship the widget",
        description: str = "The widget must ship.",
        mode: str = "COLLABORATIVE",
    ) -> dict:
        """A create-ticket body with the defaults the UI would send."""
        return {
            "title": title,
            "description": description,
            "mode": mode,
            "chat_model_config": self._model(),
        }

    def _start(self, **overrides: Any) -> str:
        """Open a ticket over HTTP and return its id."""
        created = self._client.post(
            "/factory/",
            json=self._body(**overrides),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["ticket_id"]

    def _data(self, ticket_id: str) -> dict:
        """One ticket's projected content."""
        fetched = self._client.get(
            f"/factory/{ticket_id}",
            headers=HEADERS,
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        return fetched.json()["data"]

    def _poll(
        self,
        ticket_id: str,
        statuses: set[str],
        timeout: float = 15.0,
    ) -> dict:
        """Poll a ticket until it reaches one of *statuses*."""
        deadline = time.monotonic() + timeout
        data: dict | None = None
        while time.monotonic() < deadline:
            data = self._data(ticket_id)
            if data["status"] in statuses:
                return data
            time.sleep(0.05)
        self.fail(
            f"Factory ticket {ticket_id!r} never reached {statuses}; "
            f"last status {data and data['status']!r}.",
        )

    # --------------------------------------------------------------- tests

    def test_create_walks_to_requirements_gate(self) -> None:
        """A created ticket runs its first debate and parks at the
        gate with the full projection readable."""
        ticket_id = self._start()
        # Freshly created: PENDING, or already RUNNING once the walker
        # task takes over on the app's event loop.
        self.assertIn(
            self._data(ticket_id)["status"],
            {"PENDING", "RUNNING"},
        )

        parked = self._poll(ticket_id, {"WAITING_HUMAN"})
        self.assertEqual(parked["ticket_id"], ticket_id)
        self.assertEqual(parked["title"], "ship the widget")
        self.assertEqual(parked["description"], "The widget must ship.")
        self.assertEqual(parked["mode"], "COLLABORATIVE")
        self.assertEqual(parked["phase"], "REQUIREMENTS")
        self.assertEqual(parked["trigger"], "api")
        self.assertIsNone(parked["error"])
        self.assertIsNone(parked["workspace_id"])
        self.assertIsNone(parked["review"])
        self.assertIsNone(parked["finished_at"])
        self.assertIsNotNone(parked["created_at"])
        self.assertIsNotNone(parked["updated_at"])
        # The posted model config round-trips through the projection.
        self.assertEqual(
            parked["chat_model_config"]["model"],
            "factory-model",
        )
        self.assertEqual(
            parked["chat_model_config"]["credential_id"],
            self._credential_id,
        )
        # The phase's discussion ran to a conclusion and banked it.
        discussion_id = parked["discussions"]["REQUIREMENTS"]
        self.assertTrue(discussion_id)
        self.assertEqual(parked["requirements"], _CONCLUSION_TEXT)
        decisions = parked["decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["phase"], "REQUIREMENTS")
        self.assertEqual(decisions[0]["discussion_id"], discussion_id)
        self.assertEqual(decisions[0]["conclusion"], _CONCLUSION_TEXT)
        self.assertTrue(decisions[0]["consensus"])
        self.assertEqual(decisions[0]["note"], "")
        # No DEVELOP yet: no tasks, no workspace.
        self.assertEqual(parked["tasks"], [])

    def test_approve_advances_to_the_next_gate(self) -> None:
        """Approving the requirements gate dispatches the architecture
        phase, which parks at its own gate."""
        ticket_id = self._start()
        self._poll(ticket_id, {"WAITING_HUMAN"})

        approved = self._client.post(
            f"/factory/{ticket_id}/approve",
            json={"note": "requirements ok"},
            headers=HEADERS,
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        data = approved.json()["data"]
        self.assertEqual(data["phase"], "ARCHITECTURE")
        self.assertEqual(data["status"], "RUNNING")

        parked = self._poll(ticket_id, {"WAITING_HUMAN"})
        self.assertEqual(parked["phase"], "ARCHITECTURE")
        self.assertTrue(parked["architecture"])
        self.assertTrue(parked["requirements"])
        self.assertEqual(len(parked["discussions"]), 2)
        notes = [decision["note"] for decision in parked["decisions"]]
        self.assertIn("requirements ok", notes)

    def test_reject_reruns_the_phase_with_a_fresh_discussion(self) -> None:
        """Rejecting a gate re-runs its phase from scratch: a new
        discussion replaces the rejected one."""
        ticket_id = self._start()
        parked = self._poll(ticket_id, {"WAITING_HUMAN"})
        old_id = parked["discussions"]["REQUIREMENTS"]

        rejected = self._client.post(
            f"/factory/{ticket_id}/reject",
            json={"note": "redo it"},
            headers=HEADERS,
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["data"]["status"], "RUNNING")

        rerun = self._poll(ticket_id, {"WAITING_HUMAN"})
        self.assertEqual(rerun["phase"], "REQUIREMENTS")
        self.assertNotEqual(
            rerun["discussions"]["REQUIREMENTS"],
            old_id,
        )
        self.assertTrue(rerun["requirements"])
        notes = [decision["note"] for decision in rerun["decisions"]]
        self.assertIn("redo it", notes)

    def test_cancel_mid_run_lands_cancelled(self) -> None:
        """Cancelling a walking ticket ends it CANCELLED."""
        ticket_id = self._start()

        cancelled = self._client.post(
            f"/factory/{ticket_id}/cancel",
            headers=HEADERS,
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(
            cancelled.json()["data"]["status"],
            "CANCELLED",
        )

        final = self._poll(ticket_id, {"CANCELLED"})
        self.assertIsNotNone(final["finished_at"])

    async def test_gate_answers_only_when_waiting(self) -> None:
        """Approve and reject answer 409 unless the ticket parks at a
        gate; unknown ids answer 404."""
        running = await self._storage.upsert_factory_ticket(
            "alice",
            _seeded_ticket(FactoryStatus.RUNNING),
        )
        self.assertEqual(
            self._client.post(
                f"/factory/{running.id}/approve",
                json={"note": "x"},
                headers=HEADERS,
            ).status_code,
            409,
        )
        self.assertEqual(
            self._client.post(
                f"/factory/{running.id}/reject",
                json={"note": "x"},
                headers=HEADERS,
            ).status_code,
            409,
        )

        self.assertEqual(
            self._client.get(
                "/factory/missing",
                headers=HEADERS,
            ).status_code,
            404,
        )
        for verb, body in (
            ("approve", {"note": "x"}),
            ("reject", {"note": "x"}),
            ("cancel", None),
        ):
            with self.subTest(verb):
                response = self._client.post(
                    f"/factory/missing/{verb}",
                    **({"json": body} if body is not None else {}),
                    headers=HEADERS,
                )
                self.assertEqual(response.status_code, 404)

    def test_cross_user_isolation(self) -> None:
        """Another user cannot see or answer someone else's ticket."""
        ticket_id = self._start()
        self._poll(ticket_id, {"WAITING_HUMAN"})
        bob = {"X-User-ID": "bob"}

        self.assertEqual(
            self._client.get(
                f"/factory/{ticket_id}",
                headers=bob,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                f"/factory/{ticket_id}/approve",
                json={"note": "x"},
                headers=bob,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                f"/factory/{ticket_id}/reject",
                json={},
                headers=bob,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.post(
                f"/factory/{ticket_id}/cancel",
                headers=bob,
            ).status_code,
            404,
        )
        self.assertEqual(
            self._client.get("/factory/", headers=bob).json()["total"],
            0,
        )
        # Alice's ticket is untouched by all of bob's attempts.
        self.assertEqual(
            self._data(ticket_id)["status"],
            "WAITING_HUMAN",
        )

    async def test_list_newest_first_and_status_filter(self) -> None:
        """Tickets list newest first; ``?status=`` narrows the listing
        and unknown values answer 422."""
        first = self._start()
        self._poll(first, {"WAITING_HUMAN"})
        second = self._start()
        self._poll(second, {"WAITING_HUMAN"})
        cancelled = await self._storage.upsert_factory_ticket(
            "alice",
            _seeded_ticket(FactoryStatus.CANCELLED),
        )

        listed = self._client.get("/factory/", headers=HEADERS).json()
        self.assertEqual(listed["total"], 3)
        self.assertEqual(
            [ticket["ticket_id"] for ticket in listed["tickets"]],
            [cancelled.id, second, first],
        )

        waiting = self._client.get(
            "/factory/",
            params={"status": "WAITING_HUMAN"},
            headers=HEADERS,
        ).json()
        self.assertEqual(
            [ticket["ticket_id"] for ticket in waiting["tickets"]],
            [second, first],
        )

        gone = self._client.get(
            "/factory/",
            params={"status": "CANCELLED"},
            headers=HEADERS,
        ).json()
        self.assertEqual(
            [ticket["ticket_id"] for ticket in gone["tickets"]],
            [cancelled.id],
        )

        bogus = self._client.get(
            "/factory/",
            params={"status": "BOGUS"},
            headers=HEADERS,
        )
        self.assertEqual(bogus.status_code, 422)
        detail = bogus.json()["detail"]
        self.assertIn("BOGUS", detail)
        # The 422 teaches the allowed vocabulary.
        self.assertIn("PENDING", detail)
        self.assertIn("WAITING_HUMAN", detail)
        self.assertIn("CANCELLED", detail)

    def test_create_validation_rejections(self) -> None:
        """Structural bounds answer 422 before anything is dispatched."""
        no_model = self._body()
        del no_model["chat_model_config"]
        cases = (
            (self._body(title=""), "empty title"),
            (self._body(title="   "), "whitespace title"),
            (no_model, "no model config"),
            (self._body(mode="BOGUS"), "unknown mode"),
        )
        for body, why in cases:
            with self.subTest(why):
                created = self._client.post(
                    "/factory/",
                    json=body,
                    headers=HEADERS,
                )
                self.assertEqual(created.status_code, 422, created.text)
        # Nothing above persisted anything.
        listed = self._client.get("/factory/", headers=HEADERS).json()
        self.assertEqual(listed["total"], 0)

    def test_view_covers_every_ticket_field(self) -> None:
        """The API view carries every persisted ticket field.

        The discussion endpoints embed the record's data model
        wholesale, so a new field reaches clients for free; this API
        hand-rolls its projection (``FactoryTicketView.from_record``
        lists the fields by hand), so the burden of keeping it
        complete shifts to a pin: every :class:`FactoryTicketData`
        field must appear on the view, alongside the record ids the
        view adds. A model field the view forgot fails here, not as
        a blank panel in the UI.
        """
        self.assertGreaterEqual(
            set(FactoryTicketView.model_fields),
            set(FactoryTicketData.model_fields)
            | {"ticket_id", "created_at", "updated_at"},
        )

    def test_openapi_lists_factory_routes(self) -> None:
        """The factory routes exist and never collide."""
        paths = self._client.get("/openapi.json").json()["paths"]
        self.assertIn("/factory/", paths)
        self.assertIn("post", paths["/factory/"])
        self.assertIn("get", paths["/factory/"])
        self.assertIn("/factory/roles", paths)
        self.assertIn("get", paths["/factory/roles"])
        self.assertIn("/factory/{ticket_id}", paths)
        self.assertIn("get", paths["/factory/{ticket_id}"])
        for verb in ("approve", "reject", "cancel"):
            path = f"/factory/{{ticket_id}}/{verb}"
            self.assertIn(path, paths)
            self.assertIn("post", paths[path])

    def test_roles_returns_the_roster_catalog(self) -> None:
        """GET /factory/roles returns the static catalog: three groups
        in phase order with 3/6/3 roles, every role carrying a name, a
        title and a stance, plus the shared judge."""
        response = self._client.get("/factory/roles", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        roster = response.json()
        groups = roster["groups"]
        self.assertEqual(
            [group["phase"] for group in groups],
            ["REQUIREMENTS", "ARCHITECTURE", "DEVELOP"],
        )
        self.assertEqual(
            [len(group["roles"]) for group in groups],
            [3, 6, 3],
        )
        for group in groups:
            self.assertTrue(group["title"])
            self.assertTrue(group["description"])
            for role in group["roles"]:
                self.assertTrue(role["name"])
                self.assertTrue(role["title"])
                self.assertTrue(role["stance"])
        judge = roster["judge"]
        self.assertTrue(judge["name"])
        self.assertTrue(judge["title"])
        self.assertTrue(judge["stance"])

    def test_roles_route_is_never_shadowed_by_the_ticket_id_route(
        self,
    ) -> None:
        """/factory/roles answers as the roster — a 200 carrying
        ``groups`` — and not as a 404 lookup of a ticket named
        "roles", because the fixed segment is registered before the
        ``/{ticket_id}`` routes (order-dependent matching)."""
        response = self._client.get("/factory/roles", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("groups", response.json())
        paths = [route.path for route in factory_router.routes]
        self.assertIn("/factory/roles", paths)
        self.assertIn("/factory/{ticket_id}", paths)
        self.assertLess(
            paths.index("/factory/roles"),
            paths.index("/factory/{ticket_id}"),
        )

    def test_roster_matches_what_the_engine_dispatches(self) -> None:
        """The catalog the roster shows is the catalog the engine runs:
        each debate group's role names are exactly the debater names
        ``to_debaters`` sends, in order — the catalog↔runtime pin."""
        groups = self._client.get(
            "/factory/roles",
            headers=HEADERS,
        ).json()["groups"]
        model = ChatModelConfig(**self._model())
        for group, catalog in (
            (groups[0], REQUIREMENTS_ROLES),
            (groups[1], ARCHITECTURE_ROLES),
        ):
            with self.subTest(group["phase"]):
                self.assertEqual(
                    [role["name"] for role in group["roles"]],
                    [
                        debater.name
                        for debater in to_debaters(catalog, model)
                    ],
                )


class FactoryLifespanWiringTest(IsolatedAsyncioTestCase):
    """The lifespan builds the factory engine, sweeps at boot and
    closes at shutdown."""

    def _app_backends(self) -> tuple:
        """Fakeredis backends plus the app built on them."""
        redis, storage, bus = _backends()
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        app = create_app(
            storage=storage,
            message_bus=bus,
            workspace_manager=LocalWorkspaceManager(workdir),
            enable_index_worker=False,
        )
        return redis, app

    async def test_recover_stale_fails_interrupted_at_startup(
        self,
    ) -> None:
        """Tickets a previous process left mid-flight are failed out
        as ``interrupted by restart`` before the app serves traffic."""
        redis, app = self._app_backends()
        running = await _reader(redis).upsert_factory_ticket(
            "alice",
            _seeded_ticket(FactoryStatus.RUNNING),
        )
        waiting = await _reader(redis).upsert_factory_ticket(
            "alice",
            _seeded_ticket(
                FactoryStatus.WAITING_HUMAN,
                phase=FactoryPhase.ARCHITECTURE,
            ),
        )

        with TestClient(app) as client:
            # The lifespan built the factory engine and swept with it.
            self.assertIsInstance(
                client.app.state.factory_engine,
                FactoryEngine,
            )
            for ticket_id in (running.id, waiting.id):
                fetched = client.get(
                    f"/factory/{ticket_id}",
                    headers=HEADERS,
                )
                self.assertEqual(fetched.status_code, 200, fetched.text)
                data = fetched.json()["data"]
                self.assertEqual(data["status"], "FAILED")
                self.assertEqual(data["error"], "interrupted by restart")
                self.assertIsNotNone(data["finished_at"])

    async def test_close_on_shutdown_cancels_running_walker(
        self,
    ) -> None:
        """Shutdown closes the factory engine: a walker caught
        mid-turn is cancelled and its record left mid-flight, exactly
        what the next process's sweep classifies."""
        redis, app = self._app_backends()
        _FakeFactoryModel.blocking = True
        self.addCleanup(setattr, _FakeFactoryModel, "blocking", False)

        with TestClient(app) as client:
            created = client.post(
                "/credential/",
                json={
                    "data": {
                        "type": "fake_factory_credential",
                        "name": "fake",
                    },
                },
                headers=HEADERS,
            )
            self.assertEqual(created.status_code, 201, created.text)
            ticket = client.post(
                "/factory/",
                json={
                    "title": "stuck mid-flight",
                    "description": "The model never answers.",
                    "chat_model_config": {
                        "type": "fake",
                        "credential_id": created.json()["credential_id"],
                        "model": "factory-model",
                        "parameters": {},
                    },
                },
                headers=HEADERS,
            )
            self.assertEqual(ticket.status_code, 201, ticket.text)
            ticket_id = ticket.json()["ticket_id"]

            # The walker is now inside its first debate: the phase's
            # discussion id landed on the ticket before the debate
            # ran, and the model blocks on the very first turn.
            deadline = time.monotonic() + 10.0
            data: dict | None = None
            while time.monotonic() < deadline:
                data = client.get(
                    f"/factory/{ticket_id}",
                    headers=HEADERS,
                ).json()["data"]
                if (
                    data["status"] == "RUNNING"
                    and data["discussions"].get("REQUIREMENTS")
                ):
                    break
                time.sleep(0.05)
            assert data is not None
            self.assertEqual(data["status"], "RUNNING")
            self.assertTrue(data["discussions"]["REQUIREMENTS"])

        # Shutdown closed the engine: no live walker remains and the
        # ticket never advanced again — never parked, never completed.
        # The record's exact shape is the engine's honest either/or
        # for a mid-discussion cancel: cancelled cleanly it stays
        # RUNNING, and a cancel delivered through its torn-down debate
        # fails the phase after the settle wait (the engine's own
        # contract for a close-torn debate). Both are exactly what
        # the next process's sweep classifies.
        engine = app.state.factory_engine
        self.assertTrue(engine._closed)
        self.assertEqual(engine._tasks, {})
        after = await _reader(redis).get_factory_ticket(
            "alice",
            ticket_id,
        )
        assert after is not None
        self.assertIn(
            after.data.status,
            (FactoryStatus.RUNNING, FactoryStatus.FAILED),
        )
        if after.data.status is FactoryStatus.FAILED:
            self.assertIn("did not complete", after.data.error or "")
        else:
            self.assertIsNone(after.data.finished_at)
