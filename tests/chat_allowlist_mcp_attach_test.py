# -*- coding: utf-8 -*-
"""Auto-attaching allowlisted library MCPs to a session workspace.

An agent whose tool allowlist names ``mcp__redmine__*`` expects the
redmine server in its session; before this, the workspace only ever
held the deployment defaults and library installs a user made by hand
in the chat panel — the most common "my agent has no tools" trap."""
from typing import Any
from unittest import IsolatedAsyncioTestCase

from agentscope.app._service._chat_mcp_sync import (
    attach_allowlisted_mcps,
)
from agentscope.app.storage import MCPRecord
from agentscope.mcp import HttpMCPConfig, MCPClient


def _record(
    name: str,
    *,
    enabled: bool = True,
) -> MCPRecord:
    """A library record for a stateless placeholder server."""
    return MCPRecord(
        user_id="alice",
        enabled=enabled,
        client=MCPClient(
            name=name,
            is_stateful=False,
            mcp_config=HttpMCPConfig(url=f"https://{name}.invalid/mcp"),
        ),
    )


class _FakeWorkspace:
    """Records adds; answers list_mcps from a mutable list."""

    def __init__(self, present: list[str] | None = None) -> None:
        self.present: list[str] = present or []
        self.added: list[str] = []
        self.fail_names: set[str] = set()

    async def list_mcps(self, agent_id: str, session_id: str) -> list:
        """Return one fake client per present name."""
        return [
            MCPClient(
                name=name,
                is_stateful=False,
                mcp_config=HttpMCPConfig(url="https://x.invalid/mcp"),
            )
            for name in self.present
        ]

    async def add_mcp(
        self,
        client: MCPClient,
        agent_id: str,
        session_id: str,
    ) -> None:
        """Record the add, or raise for names asked to fail."""
        if client.name in self.fail_names:
            raise RuntimeError(f"cannot start {client.name}")
        self.added.append(client.name)
        self.present.append(client.name)


class _FakeStorage:
    """Answers list_mcps from a fixed library."""

    def __init__(self, records: list[MCPRecord]) -> None:
        self._records = records

    async def list_mcps(self, user_id: str) -> list:
        """The caller's library."""
        return list(self._records)


async def _run(
    workspace: _FakeWorkspace,
    library: list[MCPRecord],
    allowlist: list[str],
) -> list[str]:
    """Invoke the sync with fakes and return the attached names."""
    return await attach_allowlisted_mcps(
        storage=_FakeStorage(library),  # type: ignore[arg-type]
        allowlist=allowlist,
        user_id="alice",
        agent_id="ag-1",
        session_id="se-1",
        workspace=workspace,  # type: ignore[arg-type]
    )


class AttachAllowlistedMcpsTest(IsolatedAsyncioTestCase):
    """The allowlist pulls its servers into the workspace."""

    async def test_a_named_server_is_attached(self) -> None:
        workspace = _FakeWorkspace()
        attached = await _run(
            workspace,
            [_record("redmine"), _record("gitlab")],
            ["mcp__redmine__list_issues"],
        )
        self.assertEqual(attached, ["redmine"])
        self.assertEqual(workspace.added, ["redmine"])

    async def test_an_empty_allowlist_attaches_nothing(self) -> None:
        """No allowlist means no opinion — the workspace is untouched."""
        workspace = _FakeWorkspace()
        attached = await _run(workspace, [_record("redmine")], [])
        self.assertEqual(attached, [])
        self.assertEqual(workspace.added, [])

    async def test_an_already_present_server_is_not_re_added(self) -> None:
        workspace = _FakeWorkspace(present=["redmine"])
        attached = await _run(
            workspace,
            [_record("redmine")],
            ["mcp__redmine__list_issues"],
        )
        self.assertEqual(attached, [])
        self.assertEqual(workspace.added, [])

    async def test_a_disabled_record_is_skipped(self) -> None:
        workspace = _FakeWorkspace()
        attached = await _run(
            workspace,
            [_record("redmine", enabled=False)],
            ["mcp__redmine__list_issues"],
        )
        self.assertEqual(attached, [])
        self.assertEqual(workspace.added, [])

    async def test_a_failing_attach_is_logged_not_raised(self) -> None:
        """One server that cannot start must not take the run down."""
        workspace = _FakeWorkspace()
        workspace.fail_names.add("redmine")
        attached = await _run(
            workspace,
            [_record("redmine"), _record("gitlab")],
            [
                "mcp__redmine__list_issues",
                "mcp__gitlab__create_issue",
            ],
        )
        self.assertEqual(attached, ["gitlab"])
        self.assertEqual(workspace.added, ["gitlab"])

    async def test_the_prefix_match_is_exact_per_server(self) -> None:
        """``mcp__redmine2__`` must not drag in a server named
        ``redmine`` — the trailing ``__`` guards the name boundary."""
        workspace = _FakeWorkspace()
        attached = await _run(
            workspace,
            [_record("redmine")],
            ["mcp__redmine2__list_issues"],
        )
        self.assertEqual(attached, [])
        self.assertEqual(workspace.added, [])
