# -*- coding: utf-8 -*-
"""Integration presets — catalog, direct create, and re-key.

Presets are the no-hub path into the MCP library: a curated catalog
served by the app itself, created straight into the library, and
re-keyed later from the stored answers rather than a hub card.
"""
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app._service import _mcp_presets
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import MCPRecord, RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import HttpMCPConfig, MCPClient

HEADERS = {"X-User-ID": "alice"}

GITLAB_VALUES = {
    "url": "https://gitlab.example.com/api/v4",
    "token": "glpat-first",
}


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


class McpPresetsTest(IsolatedAsyncioTestCase):
    """The preset catalog over HTTP, with no hub registered at all."""

    def setUp(self) -> None:
        """Start an app whose only MCP source is the preset catalog."""
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        self._storage, bus = _fake_backends()
        app = create_app(
            storage=self._storage,
            message_bus=bus,
            workspace_manager=LocalWorkspaceManager(workdir),
            enable_index_worker=False,
        )
        self._client = self.enterContext(TestClient(app))

    def _create(self, preset_id: str, **body: Any) -> Any:
        """POST a preset install into the caller's library."""
        return self._client.post(
            "/mcp",
            json={"preset_id": preset_id, **body},
            headers=HEADERS,
        )

    def _record(self, mcp_id: str) -> Any:
        """Read one record back through storage, config included."""
        storage = self._client.app.state.storage
        return storage.get_mcp("alice", mcp_id)

    # ── catalog ────────────────────────────────────────────────────

    def test_lists_the_catalog(self) -> None:
        """Both presets are listed, with their secret fields marked."""
        body = self._client.get("/mcp/presets", headers=HEADERS).json()

        self.assertEqual(body["total"], 2)
        by_id = {p["id"]: p for p in body["presets"]}
        self.assertEqual(set(by_id), {"gitlab", "redmine"})

        gitlab = {f["name"]: f for f in by_id["gitlab"]["fields"]}
        self.assertEqual(
            gitlab["token"]["label"],
            "Personal Access Token",
        )
        self.assertTrue(gitlab["token"]["secret"])
        self.assertFalse(gitlab["url"]["secret"])

        redmine = {f["name"]: f for f in by_id["redmine"]["fields"]}
        self.assertTrue(redmine["token"]["secret"])

    # ── create ─────────────────────────────────────────────────────

    async def test_create_lands_in_the_library(self) -> None:
        """A filled-in preset becomes a stateful stdio record carrying
        both values as environment variables."""
        response = self._create("gitlab", values=GITLAB_VALUES)

        self.assertEqual(response.status_code, 201)
        mcp_id = response.json()["mcp_id"]

        listed = self._client.get("/mcp", headers=HEADERS).json()
        self.assertEqual([m["name"] for m in listed], ["gitlab"])
        self.assertTrue(listed[0]["is_stateful"])
        self.assertEqual(listed[0]["hub_id"], "preset")
        self.assertEqual(listed[0]["card_id"], "gitlab")

        record = await self._record(mcp_id)
        config = record.client.mcp_config
        self.assertEqual(config.command, "npx")
        self.assertEqual(config.args, ["-y", "@zereight/mcp-gitlab"])
        self.assertEqual(
            config.env["GITLAB_API_URL"],
            "https://gitlab.example.com/api/v4",
        )
        self.assertEqual(
            config.env["GITLAB_PERSONAL_ACCESS_TOKEN"],
            "glpat-first",
        )

    async def test_create_under_a_custom_name(self) -> None:
        """The name is honored, so a clash can be sidestepped."""
        response = self._create(
            "gitlab",
            name="work-gitlab",
            values=GITLAB_VALUES,
        )

        self.assertEqual(response.status_code, 201)
        record = await self._record(response.json()["mcp_id"])
        self.assertEqual(record.client.name, "work-gitlab")

    def test_create_rejects_a_missing_field(self) -> None:
        """A required value the caller omitted is a 422 naming it."""
        response = self._create(
            "gitlab",
            values={"url": "https://gitlab.example.com/api/v4"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("token", response.json()["detail"])

    def test_create_rejects_an_illegal_name(self) -> None:
        """The name must stay within ``[a-zA-Z0-9_-]``."""
        response = self._create(
            "gitlab",
            name="my gitlab",
            values=GITLAB_VALUES,
        )

        self.assertEqual(response.status_code, 422)

    def test_create_rejects_a_duplicate_name(self) -> None:
        """The MCP name is unique per user, presets included."""
        self._create("gitlab", values=GITLAB_VALUES)
        response = self._create("gitlab", values=GITLAB_VALUES)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            len(self._client.get("/mcp", headers=HEADERS).json()),
            1,
        )

    def test_create_rejects_an_unknown_preset(self) -> None:
        """A preset id not in the catalog is a 404."""
        response = self._create("nope", values=GITLAB_VALUES)

        self.assertEqual(response.status_code, 404)

    # ── re-key ─────────────────────────────────────────────────────

    async def test_rekey_merges_over_the_stored_values(self) -> None:
        """A partial re-key replaces only what was sent: the secret is
        one env var, the URL another, and neither knows about the
        other."""
        mcp_id = self._create(
            "gitlab",
            values=GITLAB_VALUES,
        ).json()["mcp_id"]

        response = self._client.patch(
            f"/mcp/{mcp_id}",
            json={"values": {"token": "glpat-second"}},
            headers=HEADERS,
        )

        self.assertEqual(response.status_code, 200)
        record = await self._record(mcp_id)
        env = record.client.mcp_config.env
        self.assertEqual(env["GITLAB_PERSONAL_ACCESS_TOKEN"], "glpat-second")
        self.assertEqual(
            env["GITLAB_API_URL"],
            "https://gitlab.example.com/api/v4",
        )
        self.assertEqual(
            record.values,
            {
                "url": "https://gitlab.example.com/api/v4",
                "token": "glpat-second",
            },
        )

    async def test_rekey_with_the_preset_gone_from_the_catalog(
        self,
    ) -> None:
        """A preset removed from the catalog leaves its records
        un-re-keyable, like a hub that dropped its card."""
        mcp_id = self._create(
            "gitlab",
            values=GITLAB_VALUES,
        ).json()["mcp_id"]

        with patch.object(_mcp_presets, "PRESETS", []):
            response = self._client.patch(
                f"/mcp/{mcp_id}",
                json={"values": {"token": "glpat-second"}},
                headers=HEADERS,
            )

        self.assertEqual(response.status_code, 400)

    async def test_rekey_on_a_hand_added_mcp_is_refused(self) -> None:
        """A record with no preset and no hub still has no card to
        re-render from — the regression guard for the new branch."""
        hand = MCPRecord(
            user_id="alice",
            client=MCPClient(
                name="hand-rolled",
                is_stateful=False,
                mcp_config=HttpMCPConfig(
                    url="https://hand.invalid/sse",
                ),
            ),
        )
        await self._storage.upsert_mcp("alice", hand)

        response = self._client.patch(
            f"/mcp/{hand.id}",
            json={"values": {"token": "anything"}},
            headers=HEADERS,
        )

        self.assertEqual(response.status_code, 400)

    def test_rename_and_disable_need_no_card(self) -> None:
        """Renaming and toggling work on a preset record without
        consulting the catalog."""
        mcp_id = self._create(
            "gitlab",
            values=GITLAB_VALUES,
        ).json()["mcp_id"]

        response = self._client.patch(
            f"/mcp/{mcp_id}",
            json={"name": "gl-renamed", "enabled": False},
            headers=HEADERS,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "gl-renamed")
        self.assertFalse(response.json()["enabled"])
