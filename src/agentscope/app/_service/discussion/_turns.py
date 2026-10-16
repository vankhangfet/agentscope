# -*- coding: utf-8 -*-
"""The one-participant-one-turn plumbing shared by both discussion
engines.

A "participant" is a debater or the judge: each gets one agent record
(the stance rides its system prompt), one session whose
:class:`~agentscope.app.storage.DiscussionOrigin` names the discussion
and the participant, and turns driven through the chat runner on the
proven ``ChatService.run`` path. This module plays the role
``_workflow_nodes.py`` plays for the workflow engine: the low-level
session/turn mechanics both engines stand on, with no debate logic of
its own.
"""
from ...._utils._common import _generate_id
from ....message import ToolCallState, UserMsg
from ....permission import PermissionContext, PermissionMode
from ....state import AgentState
from ....types import ReplyFinishedReason
from .._workflow_nodes import ChatRunner
from ...storage import (
    AgentChatConfig,
    AgentData,
    AgentRecord,
    ChatModelConfig,
    DiscussionOrigin,
    DiscussionRecord,
    SessionConfig,
    StorageBase,
)

_PARKED_TOOL_STATES = (
    ToolCallState.PENDING,
    ToolCallState.ASKING,
    ToolCallState.SUBMITTED,
)
"""Block states meaning the turn parked on a tool-call decision.

``asking`` and ``submitted`` are what the chat and session services
treat as a parked turn; ``pending`` is belt-and-braces for a turn that
ended before the permission system even looked at the call."""

JUDGE_KEY = "judge"
"""The session/agent key of the evaluator's own participant slot."""


class TurnFailure(Exception):
    """One participant turn failed; message is user-facing."""


class ParticipantSessions:
    """One session per participant per discussion, cached in memory.

    Session (and agent) ids are cached on the instance rather than the
    discussion record: a debate is driven by one engine loop in one
    process, and the boot sweep fails out anything a crash left
    mid-flight instead of resuming it, so no path ever needs to
    re-derive a session id from storage. The cache is what keeps every
    round of one debater inside ONE conversation — arguments accumulate
    in the session history.
    """

    def __init__(self, storage: StorageBase) -> None:
        self._storage = storage
        self._cache: dict[tuple[str, str], tuple[str, str]] = {}

    async def ensure(
        self,
        user_id: str,
        record: DiscussionRecord,
        key: str,
        system_prompt: str,
        model: ChatModelConfig,
    ) -> tuple[str, str]:
        """Return ``(session_id, agent_id)`` for *key*, minting both on
        first use.

        The agent is named after the discussion and participant so the
        row is traceable to its debate (agent names are not unique per
        user, so repeated stances never collide); the session carries
        the participant's model on its config and the discussion in its
        origin.

        Args:
            user_id (`str`): The discussion's owner.
            record (`DiscussionRecord`): The discussion being argued.
            key (`str`): The participant slot — a debater's name, or
                ``"judge"``.
            system_prompt (`str`): The participant's instructions (a
                debater's stance, or the judge's).
            model (`ChatModelConfig`): The participant's model.

        Returns:
            `tuple[str, str]`: The session id and agent id to drive
                turns with.
        """
        cached = self._cache.get((record.id, key))
        if cached is not None:
            return cached
        agent_id = await self._storage.upsert_agent(
            user_id,
            AgentRecord(
                user_id=user_id,
                data=AgentData(
                    name=f"discussion:{record.id}:{key}",
                    system_prompt=system_prompt,
                    chat_config=AgentChatConfig(chat_model_config=model),
                ),
            ),
        )
        session = await self._storage.upsert_session(
            user_id=user_id,
            agent_id=agent_id,
            config=SessionConfig(
                workspace_id=_generate_id(),
                name=f"{record.data.topic} / {key}",
                chat_model_config=model,
                fallback_chat_model_config=None,
            ),
            # A debate is unattended — no one is watching to decide a
            # tool call — so a bare DEFAULT state would park the turn
            # on the first call needing approval, and the parked-call
            # health check in ``read_turn_reply`` would fail the whole
            # discussion. EXPLORE never asks: read-only tools are
            # allowed and modifications denied, so no turn can stall.
            # (The scheduler gives its unattended runs a mode for the
            # same reason.)
            state=AgentState(
                permission_context=PermissionContext(
                    mode=PermissionMode.EXPLORE,
                ),
            ),
            origin=DiscussionOrigin(
                discussion_id=record.id,
                debater=key,
            ),
        )
        pair = (session.id, agent_id)
        self._cache[(record.id, key)] = pair
        return pair


async def run_turn(
    chat_runner: ChatRunner,
    user_id: str,
    session_id: str,
    agent_id: str,
    prompt: str,
) -> None:
    """Drive one participant turn: send *prompt*, let the runner reply."""
    await chat_runner(
        user_id,
        session_id,
        agent_id,
        UserMsg(name="discussion", content=prompt),
    )


async def read_turn_reply(
    storage: StorageBase,
    user_id: str,
    session_id: str,
    who: str,
) -> str:
    """Read a turn's final reply: the last assistant message's text.

    That message also decides the turn's health, exactly like the
    workflow node path: prod ``ChatService.run`` swallows its
    exceptions and closes the reply with an error or a non-COMPLETED
    reason instead of raising, so an error-bearing turn must raise here
    rather than feed its partial text into the debate.

    Args:
        storage (`StorageBase`): Application storage.
        user_id (`str`): The discussion's owner.
        session_id (`str`): The participant's session.
        who (`str`): The participant, for failure messages.

    Returns:
        `str`: The reply's text content.

    Raises:
        `TurnFailure`: The reply bears an error, a non-COMPLETED
            finish reason, a parked tool call, or never arrived.
    """
    messages, _ = await storage.list_messages(user_id, session_id, limit=20)
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        if message.error is not None:
            raise TurnFailure(
                f"{who}'s turn failed: {message.error.message} "
                f"({message.error.type}).",
            )
        if message.finished_reason not in (
            None,
            ReplyFinishedReason.COMPLETED,
        ):
            raise TurnFailure(
                f"{who}'s turn ended as {message.finished_reason.value!r}.",
            )
        if any(
            block.state in _PARKED_TOOL_STATES
            for block in message.get_content_blocks("tool_call")
        ):
            raise TurnFailure(
                f"{who}'s turn ended with a tool call still awaiting a "
                "decision.",
            )
        return message.get_text_content() or ""
    raise TurnFailure(f"{who} ended the turn without a reply.")
