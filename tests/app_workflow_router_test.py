# -*- coding: utf-8 -*-
"""Workflow CRUD endpoints over a real app and fake backends."""
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agentscope.app import create_app
from agentscope.app.channel import ChannelBase, ChannelStatus
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import (
    ChannelBinding,
    ChannelRecord,
    RedisStorage,
    RoutingConfig,
    SessionSettings,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager

HEADERS = {"X-User-ID": "alice"}


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
    """A two-node chain over the given agent."""
    return {
        "nodes": [
            {"id": "a", "config": {"type": "agent", "agent_id": agent_id}},
            {"id": "b", "config": {"type": "agent", "agent_id": agent_id}},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "b"}],
    }


class WorkflowRouterTest(IsolatedAsyncioTestCase):
    """CRUD, version bumps and validation over HTTP."""

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

    def _data(self) -> dict:
        return {
            "name": "pipeline",
            "description": "two steps",
            "graph": _graph(self._agent_id),
        }

    def test_workflow_round_trips_through_the_api(self) -> None:
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        workflow_id = created.json()["workflow_id"]

        fetched = self._client.get(
            f"/workflow/{workflow_id}",
            headers=HEADERS,
        )
        self.assertEqual(fetched.json()["data"]["name"], "pipeline")
        self.assertEqual(fetched.json()["data"]["version"], 1)

        listed = self._client.get("/workflow/", headers=HEADERS).json()
        self.assertEqual(listed["total"], 1)

        renamed = dict(self._data(), name="pipeline v2")
        patched = self._client.patch(
            f"/workflow/{workflow_id}",
            json={"data": renamed},
            headers=HEADERS,
        )
        self.assertEqual(patched.json()["data"]["name"], "pipeline v2")
        self.assertEqual(patched.json()["data"]["version"], 2)

        self.assertEqual(
            self._client.delete(
                f"/workflow/{workflow_id}",
                headers=HEADERS,
            ).status_code,
            204,
        )
        self.assertEqual(
            self._client.get(
                f"/workflow/{workflow_id}",
                headers=HEADERS,
            ).status_code,
            404,
        )

    def test_a_cyclic_graph_is_rejected_with_422(self) -> None:
        data = dict(
            self._data(),
            graph={
                "nodes": _graph(self._agent_id)["nodes"],
                "edges": [
                    {"id": "e1", "source": "a", "target": "b"},
                    {"id": "e2", "source": "b", "target": "a"},
                ],
            },
        )
        created = self._client.post(
            "/workflow/",
            json={"data": data},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)
        self.assertTrue(
            any(
                "Cycle detected" in p
                for p in created.json()["detail"]["problems"]
            ),
        )

    def test_unknown_agent_reference_is_rejected(self) -> None:
        data = dict(self._data(), graph=_graph("ghost"))
        created = self._client.post(
            "/workflow/",
            json={"data": data},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)
        self.assertTrue(
            any(
                "unknown agent" in p
                for p in created.json()["detail"]["problems"]
            ),
        )

    def test_workflows_are_scoped_per_user(self) -> None:
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        ).json()
        other = {"X-User-ID": "bob"}
        self.assertEqual(
            self._client.get(
                f"/workflow/{created['workflow_id']}",
                headers=other,
            ).status_code,
            404,
        )
        listed = self._client.get("/workflow/", headers=other).json()
        self.assertEqual(listed["total"], 0)

    def test_patch_unknown_workflow_is_404(self) -> None:
        """PATCH on a missing id answers 404, before validation."""
        patched = self._client.patch(
            "/workflow/wf-missing",
            json={"data": self._data()},
            headers=HEADERS,
        )
        self.assertEqual(patched.status_code, 404)

    def test_patch_invalid_graph_is_422(self) -> None:
        """PATCH validates the new graph before persisting."""
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        ).json()
        cyclic = dict(
            self._data(),
            graph={
                "nodes": _graph(self._agent_id)["nodes"],
                "edges": [
                    {"id": "e1", "source": "a", "target": "b"},
                    {"id": "e2", "source": "b", "target": "a"},
                ],
            },
        )
        patched = self._client.patch(
            f"/workflow/{created['workflow_id']}",
            json={"data": cyclic},
            headers=HEADERS,
        )
        self.assertEqual(patched.status_code, 422)
        # The stored record is untouched: still version 1, old name.
        fetched = self._client.get(
            f"/workflow/{created['workflow_id']}",
            headers=HEADERS,
        ).json()
        self.assertEqual(fetched["data"]["version"], 1)
        self.assertEqual(fetched["data"]["name"], "pipeline")

    def test_delete_unknown_workflow_is_404(self) -> None:
        """DELETE on a missing id answers 404; double delete too."""
        self.assertEqual(
            self._client.delete(
                "/workflow/wf-missing",
                headers=HEADERS,
            ).status_code,
            404,
        )
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        ).json()
        self.assertEqual(
            self._client.delete(
                f"/workflow/{created['workflow_id']}",
                headers=HEADERS,
            ).status_code,
            204,
        )
        self.assertEqual(
            self._client.delete(
                f"/workflow/{created['workflow_id']}",
                headers=HEADERS,
            ).status_code,
            404,
        )


class _WiringChannel(ChannelBase):
    """A registered-but-inert channel type, to wire the real factory."""

    channel_type = "wiring-fake"
    display_name = "Wiring Fake"
    platform_bot_id_field = "bot_id"

    class Credentials(BaseModel):
        """Credentials for the fake platform."""

        bot_id: str

    class Config(BaseModel):
        """Options for the fake platform."""

    def __init__(
        self,
        channel_id: str,
        credentials: "Credentials",
        config: "Config",  # pylint: disable=unused-argument
    ) -> None:
        """Store the identity; nothing else is ever called."""
        self._channel_id = channel_id
        self._bot_id = credentials.bot_id
        self.status = ChannelStatus()

    @property
    def channel_id(self) -> str:
        """The fixed test id."""
        return self._channel_id

    async def start_listening(self, emit: Any) -> None:
        """Never called."""

    async def send_response(self, event: Any, events: Any) -> None:
        """Never called."""


def _notify_graph(agent_id: str, channel_id: str) -> dict:
    """An agent node feeding a notify node."""
    return {
        "nodes": [
            {"id": "a", "config": {"type": "agent", "agent_id": agent_id}},
            {
                "id": "ping",
                "config": {
                    "type": "notify",
                    "channel_id": channel_id,
                    "message_template": "Done: {{input.topic}}",
                },
            },
        ],
        "edges": [{"id": "e1", "source": "a", "target": "ping"}],
    }


async def _seed_channel(
    storage: RedisStorage,
    *,
    channel_id: str,
    user_id: str,
    enabled: bool,
    name: str = "Ops room",
) -> ChannelRecord:
    """Insert one channel record directly, bypassing the channel API."""
    record = ChannelRecord(
        id=channel_id,
        channel_type="wiring-fake",
        name=name,
        user_id=user_id,
        enabled=enabled,
        credentials={"bot_id": f"bot-{channel_id}"},
        routing=RoutingConfig(
            bindings=[ChannelBinding(match_value="*", agent_id="agent-x")],
        ),
        session=SessionSettings(chat_model_config={"type": "x"}),
    )
    await storage.upsert_channel(
        record,
        platform_bot_id=f"bot-{channel_id}",
    )
    return record


class NotifyChannelValidationTest(IsolatedAsyncioTestCase):
    """A notify node's channel reference is checked at save time."""

    async def asyncSetUp(self) -> None:
        self._tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage, self.bus = _fake_backends()
        # Seeded before the app boots, so this test's event loop is the
        # only one touching fakeredis while the records are written;
        # entering storage first binds its client (the app's own
        # context entry re-binds the same one, so the double entry is
        # harmless).
        # pylint: disable-next=unnecessary-dunder-call
        await self.storage.__aenter__()
        await _seed_channel(
            self.storage,
            channel_id="ch-off",
            user_id="alice",
            enabled=False,
        )
        await _seed_channel(
            self.storage,
            channel_id="ch-bobs",
            user_id="bob",
            enabled=True,
        )
        await _seed_channel(
            self.storage,
            channel_id="ch-ok",
            user_id="alice",
            enabled=True,
        )
        self.app = create_app(
            storage=self.storage,
            message_bus=self.bus,
            workspace_manager=LocalWorkspaceManager(self._tmp),
            enable_index_worker=False,
        )
        self._client = self.enterContext(TestClient(self.app))
        self._agent_id = self._client.post(
            "/agent/",
            json={"name": "worker", "system_prompt": "hi"},
            headers=HEADERS,
        ).json()["agent_id"]

    def _create(self, channel_id: str) -> Any:
        return self._client.post(
            "/workflow/",
            json={
                "data": {
                    "name": "notify pipeline",
                    "graph": _notify_graph(self._agent_id, channel_id),
                },
            },
            headers=HEADERS,
        )

    async def test_unknown_channel_is_rejected(self) -> None:
        created = self._create("ch-ghost")
        self.assertEqual(created.status_code, 422)
        problems = created.json()["detail"]["problems"]
        self.assertTrue(
            any("unknown channel 'ch-ghost'" in p for p in problems),
            problems,
        )

    async def test_disabled_channel_is_rejected(self) -> None:
        await _seed_channel(
            self.storage,
            channel_id="ch-off",
            user_id="alice",
            enabled=False,
        )
        created = self._create("ch-off")
        self.assertEqual(created.status_code, 422)
        problems = created.json()["detail"]["problems"]
        self.assertTrue(
            any("disabled channel 'ch-off'" in p for p in problems),
            problems,
        )

    async def test_another_users_channel_reads_as_unknown(self) -> None:
        await _seed_channel(
            self.storage,
            channel_id="ch-bobs",
            user_id="bob",
            enabled=True,
        )
        created = self._create("ch-bobs")
        self.assertEqual(created.status_code, 422)
        problems = created.json()["detail"]["problems"]
        self.assertTrue(
            any("unknown channel 'ch-bobs'" in p for p in problems),
            problems,
        )

    async def test_enabled_owned_channel_saves(self) -> None:
        await _seed_channel(
            self.storage,
            channel_id="ch-ok",
            user_id="alice",
            enabled=True,
        )
        created = self._create("ch-ok")
        self.assertEqual(created.status_code, 201, created.text)
        workflow_id = created.json()["workflow_id"]
        fetched = self._client.get(
            f"/workflow/{workflow_id}",
            headers=HEADERS,
        )
        nodes = fetched.json()["data"]["graph"]["nodes"]
        self.assertEqual(nodes[1]["config"]["type"], "notify")


class WorkflowEngineWiringTest(IsolatedAsyncioTestCase):
    """The lifespan hands the engine the channel clients it builds."""

    async def test_engine_receives_the_lifespan_channel_clients(
        self,
    ) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        app = create_app(
            storage=storage,
            message_bus=bus,
            workspace_manager=LocalWorkspaceManager(tmp),
            enable_index_worker=False,
            channels=[_WiringChannel],
            enable_channel_worker=False,
        )
        with TestClient(app):
            # pylint: disable=protected-access
            self.assertIsNotNone(app.state.channel_clients)
            self.assertIs(
                app.state.workflow_engine._handlers._channel_clients,
                app.state.channel_clients,
            )

    async def test_engine_without_channels_gets_none(self) -> None:
        """A process with no registered channel types has no factory to
        hand over; the engine still builds and notify nodes fail at
        dispatch with the missing-channel error."""
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        app = create_app(
            storage=storage,
            message_bus=bus,
            workspace_manager=LocalWorkspaceManager(tmp),
            enable_index_worker=False,
        )
        with TestClient(app):
            # pylint: disable=protected-access
            self.assertIsNone(app.state.channel_clients)
            self.assertIsNone(
                app.state.workflow_engine._handlers._channel_clients,
            )
