# -*- coding: utf-8 -*-
"""The connection-test endpoint, ``POST /mcp/{mcp_id}/test``.

This is the "does my integration actually work" button on the MCP Hub
page's "My MCP" list. Unlike install/re-key — which never dial the
server — it walks a throwaway copy of the record's client through
connect/list/close and reports the outcome as data: ``ok=True`` with a
tool count, or ``ok=False`` with the reason, both at HTTP 200. A missing
record is the only hard error (404).
"""
import asyncio
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import MCPRecord, RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import HttpMCPConfig, MCPClient

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


def _record(name: str, stateful: bool = False) -> MCPRecord:
    """A library record whose client never needs a real connection."""
    return MCPRecord(
        user_id="alice",
        client=MCPClient(
            name=name,
            is_stateful=stateful,
            mcp_config=HttpMCPConfig(url=f"https://{name}.invalid/mcp"),
        ),
    )


class McpTestEndpointTest(IsolatedAsyncioTestCase):
    """The connection-test endpoint over HTTP."""

    def setUp(self) -> None:
        """Start an app over fakeredis, with no MCPs installed yet."""
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        self._storage, bus = _fake_backends()
        app = create_app(
            storage=self._storage,
            message_bus=bus,
            workspace_manager=LocalWorkspaceManager(workdir),
            enable_index_worker=False,
        )
        self._client = self.enterContext(TestClient(app))

    async def _install(self, record: MCPRecord) -> str:
        """Put one record in alice's library, returning its id."""
        return await self._storage.upsert_mcp("alice", record)

    def _test(self, mcp_id: str) -> Any:
        """POST the connection test for an installed MCP."""
        return self._client.post(f"/mcp/{mcp_id}/test", headers=HEADERS)

    def test_a_missing_mcp_is_a_404(self) -> None:
        """Testing an id that is not in the library is a hard error,
        the one case that is not reported as ``ok=False`` data."""
        response = self._test("does-not-exist")
        self.assertEqual(response.status_code, 404)

    async def test_a_reachable_mcp_reports_ok_with_a_tool_count(
        self,
    ) -> None:
        """A server that answers a listing is ``ok=True`` and the tool
        count rides along in both the field and the detail."""
        mcp_id = await self._install(_record("alpha"))

        async def two_tools(self: MCPClient) -> list:
            return [
                SimpleNamespace(name="mcp__alpha__search", description=""),
                SimpleNamespace(name="mcp__alpha__read", description=""),
            ]

        with patch.object(MCPClient, "list_tools", two_tools):
            response = self._test(mcp_id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool_count"], 2)
        self.assertIn("2", body["detail"])

    async def test_a_reachable_mcp_with_no_tools_is_still_ok(self) -> None:
        """Connecting but exposing nothing is a success, not a failure —
        the count is simply zero."""
        mcp_id = await self._install(_record("empty"))

        async def no_tools(self: MCPClient) -> list:
            return []

        with patch.object(MCPClient, "list_tools", no_tools):
            response = self._test(mcp_id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool_count"], 0)

    async def test_an_unreachable_mcp_reports_not_ok_with_a_reason(
        self,
    ) -> None:
        """A server that fails to answer is ``ok=False`` at HTTP 200,
        with the reason in ``detail`` and a zero count — so the UI can
        show it inline rather than catching a thrown request error."""
        mcp_id = await self._install(_record("broken"))

        async def never_answers(self: MCPClient) -> list:
            raise RuntimeError("not connected")

        with patch.object(MCPClient, "list_tools", never_answers):
            response = self._test(mcp_id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["tool_count"], 0)
        self.assertIn("not connected", body["detail"])

    async def test_a_timed_out_connection_reports_a_timeout(self) -> None:
        """A server that does not answer within the budget is a distinct
        timeout message, not a generic failure."""
        mcp_id = await self._install(_record("slow"))

        async def hangs(self: MCPClient) -> list:
            raise asyncio.TimeoutError

        with patch.object(MCPClient, "list_tools", hangs):
            response = self._test(mcp_id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertIn("Timed out", body["detail"])

    async def test_a_stateful_mcp_is_connected_then_closed(self) -> None:
        """A stateful record is walked through connect/list/close on a
        single throwaway client, exactly like the per-group listing."""
        mcp_id = await self._install(_record("gamma", stateful=True))

        seen: list[str] = []
        clients: list[int] = []

        async def fake_connect(self: MCPClient) -> None:
            seen.append("connect")
            clients.append(id(self))

        async def fake_list_tools(self: MCPClient) -> list:
            seen.append("list")
            clients.append(id(self))
            return [SimpleNamespace(name="mcp__gamma__x", description="")]

        async def fake_close(
            self: MCPClient,
            ignore_errors: bool = True,
        ) -> None:
            seen.append("close")
            clients.append(id(self))

        with (
            patch.object(MCPClient, "connect", fake_connect),
            patch.object(MCPClient, "list_tools", fake_list_tools),
            patch.object(MCPClient, "close", fake_close),
        ):
            response = self._test(mcp_id)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool_count"], 1)
        self.assertEqual(seen, ["connect", "list", "close"])
        self.assertEqual(len(set(clients)), 1)
