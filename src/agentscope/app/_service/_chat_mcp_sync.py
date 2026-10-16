# -*- coding: utf-8 -*-
"""Attaching an agent's allowlisted library MCPs to its session.

An agent's ``chat_config.tools`` names workspace MCP tools —
``mcp__{server}__{tool}`` — but a session workspace only ever held the
deployment defaults plus whatever a user attached by hand in the chat
panel. The gap between "the agent is configured for redmine" and "the
session actually has redmine" was the most common way an agent ended
up with no MCP tools at all. This sync closes it: on every run, each
enabled library MCP whose tools the agent names is pulled into the
session workspace if it is not there already.

Attaching is best-effort and idempotent: a server that fails to start
is logged and skipped — the run proceeds without it, exactly like a
hand attach through the chat panel. Unlike that manual attach, a
disabled library record is honoured here: auto-attachment follows the
disable toggle, manual attachment is explicit intent."""
import logging
from typing import TYPE_CHECKING

from ..storage import StorageBase

if TYPE_CHECKING:
    from ..workspace import WorkspaceBase

logger = logging.getLogger(__name__)


async def attach_allowlisted_mcps(
    *,
    storage: StorageBase,
    allowlist: list[str],
    user_id: str,
    agent_id: str,
    session_id: str,
    workspace: "WorkspaceBase",
) -> list[str]:
    """Pull library MCPs the agent's tool allowlist names into the
    session workspace, if they are not attached yet.

    Args:
        storage (`StorageBase`): Application storage; the MCP library.
        allowlist (`list[str]`): The agent's ``chat_config.tools``;
            empty means no opinion and attaches nothing.
        user_id (`str`): The caller's user id.
        agent_id (`str`): The running agent's id.
        session_id (`str`): The running session's id.
        workspace (`WorkspaceBase`): The session's resolved workspace.

    Returns:
        `list[str]`: The names of the MCPs attached by this call.
    """
    if not allowlist:
        return []

    library = await storage.list_mcps(user_id)
    present = {
        client.name
        for client in await workspace.list_mcps(
            agent_id=agent_id,
            session_id=session_id,
        )
    }

    attached: list[str] = []
    for record in library:
        if not record.enabled:
            continue
        client = record.client
        if client.name in present:
            continue
        # ``mcp__{name}__{tool}`` — the trailing ``__`` makes the
        # server-name boundary exact (``redmine2`` never matches a
        # server named ``redmine``).
        wanted = any(
            name.startswith(f"mcp__{client.name}__")
            for name in allowlist
        )
        if not wanted:
            continue
        try:
            await workspace.add_mcp(
                client,
                agent_id=agent_id,
                session_id=session_id,
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(
                "Attaching allowlisted MCP %r for agent %r failed: %r",
                client.name,
                agent_id,
                e,
            )
            continue
        attached.append(client.name)
        logger.info(
            "Attached allowlisted MCP %r to session %s",
            client.name,
            session_id,
        )
    return attached
