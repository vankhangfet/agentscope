# -*- coding: utf-8 -*-
"""The user-level available-tools listing, ``GET /mcp/tools``.

Unlike ``GET /workspace/mcp``, which reports the live clients of one
running session, this listing works from the library alone: it is what
the agent form's tool picker offers before any session exists.

The fake ``list_tools`` serves the namespaced tool names production
serves — ``mcp__{server}__{tool}``, per the MCPTool adapter — because
those names are what the agent's ``tools`` list keys on; a regression
back to raw server names must fail here, not pass silently.
"""
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


def _record(
    name: str,
    enabled: bool = True,
    stateful: bool = False,
) -> MCPRecord:
    """A library record whose client never needs a real connection.

    The tests answer ``list_tools`` at the class level, so the config's
    URL is only a placeholder — nothing dials it.
    """
    return MCPRecord(
        user_id="alice",
        enabled=enabled,
        client=MCPClient(
            name=name,
            is_stateful=stateful,
            mcp_config=HttpMCPConfig(url=f"https://{name}.invalid/mcp"),
        ),
    )


class McpToolsTest(IsolatedAsyncioTestCase):
    """The available-tools listing over HTTP."""

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

    async def _install(self, *records: MCPRecord) -> None:
        """Put records in alice's library directly."""
        for record in records:
            await self._storage.upsert_mcp("alice", record)

    def _tools(self) -> Any:
        """GET the available-tools listing."""
        return self._client.get("/mcp/tools", headers=HEADERS).json()

    def test_an_empty_library_lists_nothing(self) -> None:
        """No installed MCPs means no groups and a zero total."""
        self.assertEqual(self._tools(), {"groups": [], "total": 0})

    async def test_an_enabled_mcp_lists_its_tools(self) -> None:
        """One enabled MCP becomes one group of its tool names,
        namespaced the way the agent's `tools` list expects them."""
        await self._install(_record("alpha"))
        with patch.object(
            MCPClient,
            "list_tools",
            new=self._serving(
                alpha=[("search", "Search issues"), ("read", "Read a file")],
            ),
        ):
            body = self._tools()

        self.assertEqual(len(body["groups"]), 1)
        group = body["groups"][0]
        self.assertEqual(group["name"], "alpha")
        self.assertEqual(
            group["tools"],
            [
                {
                    "name": "mcp__alpha__search",
                    "description": "Search issues",
                },
                {"name": "mcp__alpha__read", "description": "Read a file"},
            ],
        )
        self.assertEqual(body["total"], 2)

    async def test_groups_are_sorted_by_mcp_name(self) -> None:
        """Storage order is arbitrary (a redis set), so the listing
        imposes one: groups come back sorted by MCP name."""
        await self._install(
            _record("zulu"),
            _record("alpha"),
            _record("mike", enabled=False),
        )
        with patch.object(
            MCPClient,
            "list_tools",
            new=self._serving(
                zulu=[("kick", "Kick a ball")],
                alpha=[("search", "Search issues")],
            ),
        ):
            body = self._tools()

        self.assertEqual(
            [g["name"] for g in body["groups"]],
            ["alpha", "zulu"],
        )

    async def test_a_disabled_mcp_is_left_out(self) -> None:
        """A turned-off MCP offers no tools, so it gets no group."""
        await self._install(_record("alpha"), _record("beta", enabled=False))
        with patch.object(
            MCPClient,
            "list_tools",
            new=self._serving(alpha=[("search", "Search issues")]),
        ):
            body = self._tools()

        self.assertEqual([g["name"] for g in body["groups"]], ["alpha"])
        self.assertEqual(body["total"], 1)

    async def test_a_stateful_mcp_is_connected_for_its_listing(self) -> None:
        """A stateful record — the default for preset and hub installs —
        cannot answer without a session, so the listing walks a
        throwaway copy through connect, list, close."""
        await self._install(_record("gamma", stateful=True))

        seen: list[str] = []
        clients: list[int] = []

        async def fake_connect(self: MCPClient) -> None:
            seen.append("connect")
            clients.append(id(self))

        async def fake_list_tools(self: MCPClient) -> list:
            seen.append("list")
            clients.append(id(self))
            return [
                SimpleNamespace(
                    name=f"mcp__{self.name}__search",
                    description="Search issues",
                ),
            ]

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
            body = self._tools()

        self.assertEqual(
            body["groups"],
            [
                {
                    "name": "gamma",
                    "tools": [
                        {
                            "name": "mcp__gamma__search",
                            "description": "Search issues",
                        },
                    ],
                },
            ],
        )
        # One throwaway client, walked through its whole lifecycle in
        # order — and closed even though the listing succeeded.
        self.assertEqual(seen, ["connect", "list", "close"])
        self.assertEqual(len(set(clients)), 1)

    async def test_an_unreachable_mcp_contributes_an_empty_group(
        self,
    ) -> None:
        """A server that fails or hangs must not sink the others: its
        group stands empty and the healthy groups stay intact."""
        await self._install(_record("alpha"), _record("broken"))

        async def never_answers(self: MCPClient) -> list:
            raise RuntimeError("not connected")

        with patch.object(
            MCPClient,
            "list_tools",
            new=self._serving(
                alpha=[("search", "Search issues")],
                broken=never_answers,
            ),
        ):
            body = self._tools()

        by_name = {g["name"]: g["tools"] for g in body["groups"]}
        self.assertEqual(
            by_name["alpha"],
            [{"name": "mcp__alpha__search", "description": "Search issues"}],
        )
        self.assertEqual(by_name["broken"], [])
        # Only real tools count, the broken server's none of them.
        self.assertEqual(body["total"], 1)

    @staticmethod
    def _serving(**tools_by_mcp: Any) -> Any:
        """A class-level ``list_tools`` stand-in serving canned answers.

        Each keyword is an MCP name mapped to its ``(name, description)``
        pairs, except a mapping to an async callable, which is used as
        that MCP's ``list_tools`` verbatim — the hook for making one
        server raise. Every other MCP gets an empty tool list. Served
        names are namespaced (``mcp__{server}__{tool}``) to match what
        production answers.
        """

        async def fake_list_tools(self: MCPClient) -> list:
            answer = tools_by_mcp.get(self.name, [])
            if callable(answer):
                return await answer(self)
            return [
                SimpleNamespace(
                    name=f"mcp__{self.name}__{name}",
                    description=description,
                )
                for name, description in answer
            ]

        return fake_list_tools
