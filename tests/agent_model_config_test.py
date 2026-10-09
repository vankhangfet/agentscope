# -*- coding: utf-8 -*-
"""Agents can carry their own model config, tools and memory flag —
the fields that turn an agent into a reusable template.

Since the ``chat_config`` grouping, these live inside
:class:`AgentChatConfig`; flat request bodies are folded in by
``AgentData``'s legacy-field validator, which these tests exercise."""
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import AgentData, RedisStorage
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


class AgentModelConfigTest(IsolatedAsyncioTestCase):
    """The template fields round-trip through the agent API.

    There is no ``GET /agent/{id}`` endpoint, so every read-back goes
    through the list endpoint and picks the record by id — which also
    proves the fields survive a storage round-trip, not just the
    request echo.
    """

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

    def _fetch_data(self, agent_id: str) -> dict:
        """Read one agent's data back from storage via the list."""
        listed = self._client.get("/agent/", headers=HEADERS).json()
        record = next(a for a in listed["agents"] if a["id"] == agent_id)
        return record["data"]

    def _fetch_chat_config(self, agent_id: str) -> dict:
        """Read one agent's ``chat_config`` block back from storage."""
        return self._fetch_data(agent_id)["chat_config"]

    def test_agent_carries_model_config_tools_and_memory(self) -> None:
        created = self._client.post(
            "/agent/",
            json={
                "name": "writer",
                "system_prompt": "Write reports.",
                "chat_model_config": {
                    "type": "openai_credential",
                    "credential_id": "c-1",
                    "model": "gpt-4o",
                    "parameters": {"temperature": 0.4},
                },
                "tools": ["web", "files"],
                "memory": True,
            },
            headers=HEADERS,
        )
        # The create endpoint returns 201 Created (FastAPI default for
        # POST is overridden explicitly in the router).
        self.assertEqual(created.status_code, 201)
        chat_config = self._fetch_chat_config(created.json()["agent_id"])
        self.assertEqual(chat_config["chat_model_config"]["model"], "gpt-4o")
        self.assertEqual(chat_config["tools"], ["web", "files"])
        self.assertTrue(chat_config["memory"])

    def test_new_fields_default_to_none(self) -> None:
        created = self._client.post(
            "/agent/",
            json={"name": "plain"},
            headers=HEADERS,
        ).json()
        chat_config = self._fetch_chat_config(created["agent_id"])
        self.assertIsNone(chat_config["chat_model_config"])
        self.assertEqual(chat_config["tools"], [])
        self.assertFalse(chat_config["memory"])

    def test_patch_updates_the_new_fields(self) -> None:
        created = self._client.post(
            "/agent/",
            json={"name": "writer"},
            headers=HEADERS,
        ).json()
        agent_id = created["agent_id"]
        patched = self._client.patch(
            f"/agent/{agent_id}",
            json={
                "chat_model_config": {
                    "type": "dashscope_credential",
                    "credential_id": "c-2",
                    "model": "qwen-max",
                    "parameters": {},
                },
                "tools": ["web"],
                "memory": True,
            },
            headers=HEADERS,
        )
        self.assertEqual(patched.status_code, 200)
        chat_config = self._fetch_chat_config(agent_id)
        self.assertEqual(chat_config["chat_model_config"]["model"], "qwen-max")
        self.assertTrue(chat_config["memory"])

    def test_patch_can_clear_model_config_with_explicit_null(self) -> None:
        created = self._client.post(
            "/agent/",
            json={
                "name": "writer",
                "chat_model_config": {
                    "type": "openai_credential",
                    "credential_id": "c-1",
                    "model": "gpt-4o",
                    "parameters": {},
                },
                "tools": ["web"],
            },
            headers=HEADERS,
        ).json()
        agent_id = created["agent_id"]
        patched = self._client.patch(
            f"/agent/{agent_id}",
            json={"chat_model_config": None},
            headers=HEADERS,
        )
        self.assertEqual(patched.status_code, 200)
        chat_config = self._fetch_chat_config(agent_id)
        # Null is a persisted state in its own right — "the session
        # level config is in charge" — not just an omitted field.
        self.assertIsNone(chat_config["chat_model_config"])
        self.assertEqual(self._fetch_data(agent_id)["name"], "writer")
        self.assertEqual(chat_config["tools"], ["web"])

    def test_partial_patch_preserves_other_fields(self) -> None:
        created = self._client.post(
            "/agent/",
            json={
                "name": "writer",
                "chat_model_config": {
                    "type": "openai_credential",
                    "credential_id": "c-1",
                    "model": "gpt-4o",
                    "parameters": {},
                },
                "tools": ["web", "files"],
                "memory": True,
            },
            headers=HEADERS,
        ).json()
        agent_id = created["agent_id"]
        patched = self._client.patch(
            f"/agent/{agent_id}",
            json={"tools": ["web"]},
            headers=HEADERS,
        )
        self.assertEqual(patched.status_code, 200)
        chat_config = self._fetch_chat_config(agent_id)
        # Only ``tools`` moved; the omitted fields keep their values.
        self.assertEqual(chat_config["tools"], ["web"])
        self.assertEqual(chat_config["chat_model_config"]["model"], "gpt-4o")
        self.assertTrue(chat_config["memory"])

    def test_legacy_record_without_new_fields_still_validates(self) -> None:
        # A record persisted before these fields existed carries no
        # ``chat_model_config`` / ``tools`` / ``memory`` keys at all.
        # ``context_config`` and ``react_config`` have been required
        # since long before, so a genuine legacy payload still has them.
        data = AgentData.model_validate(
            {
                "id": "x",
                "name": "old",
                "system_prompt": "hi",
                "context_config": {},
                "react_config": {},
            },
        )
        self.assertIsNone(data.chat_config.chat_model_config)
        self.assertEqual(data.chat_config.tools, [])
        self.assertFalse(data.chat_config.memory)
