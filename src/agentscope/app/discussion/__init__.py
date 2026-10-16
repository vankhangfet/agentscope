# -*- coding: utf-8 -*-
"""The public discussion facade: start a debate in one call.

This is the thin, stable surface callers depend on — validate the
payload, persist the PENDING record, dispatch the engine, return the
record. Every piece of debate logic lives in the engines under
:mod:`agentscope.app._service.discussion`.
"""
from .._service._workflow_nodes import ChatRunner
from .._service.discussion import DebateEngine
from ..storage import (
    DebaterConfig,
    DiscussionData,
    DiscussionRecord,
    JudgeConfig,
    StorageBase,
)

_ENGINE: DebateEngine | None = None
"""The process-wide engine the facade dispatches through, set by the
app lifespan; ``None`` makes the facade build a one-shot engine around
the caller's storage and chat runner (the debate still runs to
completion, only engine-level cancel/close/recovery are out of reach).
"""


def configure_discussion(engine: DebateEngine | None) -> None:
    """Register the engine the facade dispatches through.

    The app lifespan calls this once at startup with the engine it
    owns; teardown is that engine's
    :meth:`~agentscope.app._service.discussion.DebateEngine.close`,
    not a re-registration. Tests use it to point the facade at a
    hand-built engine, and clear it again by passing ``None``.

    Args:
        engine (`DebateEngine | None`): The engine to dispatch through,
            or ``None`` to go back to one-shot engines.
    """
    global _ENGINE  # pylint: disable=global-statement
    _ENGINE = engine


async def create_and_dispatch(
    user_id: str,
    *,
    engine: DebateEngine,
    storage: StorageBase,
    topic: str,
    debaters: list[DebaterConfig],
    judge: JudgeConfig | None = None,
    max_rounds: int = 3,
    trigger: str = "manual",
) -> DiscussionRecord:
    """Validate, persist PENDING and dispatch — one source of truth.

    Both creators of discussions stand here — the facade and the REST
    router — so the payload rules (non-empty topic, 1 to 6 uniquely
    named debaters, ``max_rounds`` within 1..10) and the judge default
    live exactly once. The record is durable before the engine is
    dispatched, so a crash in between leaves a PENDING discussion the
    boot sweep fails out honestly.

    Args:
        user_id (`str`): The owner the discussion belongs to.
        engine (`DebateEngine`): The engine to dispatch through.
        storage (`StorageBase`): Where the record persists.
        topic (`str`): The issue under debate; non-empty.
        debaters (`list[DebaterConfig]`): Who argues — 1 to 6, each a
            name (unique within the discussion), a stance and a model.
        judge (`JudgeConfig | None`, optional): The evaluator; ``None``
            keeps the default neutral prompt and runs the judge on the
            first debater's model.
        max_rounds (`int`, optional): Round ceiling, 1 to 10; the
            debate may stop earlier on judge consensus.
        trigger (`str`, optional): What started the discussion
            (``manual`` | ``api`` | ``chat_tool``), recorded on it.

    Returns:
        `DiscussionRecord`: The persisted record as written — PENDING,
            with its storage-assigned id.

    Raises:
        `ValueError`: The topic is empty, the debater count is outside
            1..6, two debaters share a name, or ``max_rounds`` is
            outside 1..10 (the record model's own bounds).
    """
    if not topic or not topic.strip():
        raise ValueError("topic must be a non-empty string.")
    if not 1 <= len(debaters) <= 6:
        raise ValueError(
            f"debaters must hold 1 to 6 entries, got {len(debaters)}.",
        )
    names = [debater.name for debater in debaters]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ValueError(
            f"debater names must be unique within a discussion; "
            f"repeated: {sorted(duplicates)}.",
        )
    resolved_judge = judge or JudgeConfig(
        # No judge model given: run the evaluator on the first
        # debater's model — the debate is already configured to pay
        # for that one.
        chat_model_config=debaters[0].chat_model_config,
    )
    record = DiscussionRecord(
        user_id=user_id,
        data=DiscussionData(
            topic=topic,
            max_rounds=max_rounds,
            debaters=list(debaters),
            judge=resolved_judge,
            trigger=trigger,
        ),
    )
    record = await storage.upsert_discussion(user_id, record)
    await engine.dispatch(user_id, record.id)
    return record


async def discussion(
    user_id: str,
    topic: str,
    *,
    debaters: list[DebaterConfig],
    judge: JudgeConfig | None = None,
    max_rounds: int = 3,
    storage: StorageBase,
    chat_runner: ChatRunner,
    trigger: str = "manual",
) -> DiscussionRecord:
    """Start a debate and return its persisted PENDING record.

    The registered engine (set by the app lifespan through
    :func:`configure_discussion`) dispatches when there is one;
    without one a one-shot engine wraps the caller's storage and chat
    runner and the debate still runs to completion, only engine-level
    cancel / close / recovery are out of reach. The record is durable
    before the engine is dispatched, so a crash in between leaves a
    PENDING discussion the boot sweep fails out honestly. The debate
    itself runs in the background; poll the record (by id, through
    the same storage) for rounds and status.

    Args:
        user_id (`str`): The owner the discussion belongs to.
        topic (`str`): The issue under debate; non-empty.
        debaters (`list[DebaterConfig]`): Who argues — 1 to 6, each a
            name (unique within the discussion), a stance and a model.
        judge (`JudgeConfig | None`, optional): The evaluator; ``None``
            keeps the default neutral prompt and runs the judge on the
            first debater's model.
        max_rounds (`int`, optional): Round ceiling, 1 to 10; the
            debate may stop earlier on judge consensus.
        storage (`StorageBase`): Where the record persists.
        chat_runner (`ChatRunner`): Drives one agent turn to
            completion (``ChatService.run`` in production); used only
            when no engine is registered.
        trigger (`str`, optional): What started the discussion
            (``manual`` | ``api`` | ``chat_tool``), recorded on it.

    Returns:
        `DiscussionRecord`: The persisted record as written — PENDING,
        with its storage-assigned id.

    Raises:
        `ValueError`: The topic is empty, the debater count is outside
            1..6, two debaters share a name, or ``max_rounds`` is
            outside 1..10 (the record model's own bounds).
    """
    engine = _ENGINE
    if engine is None:
        engine = DebateEngine(storage, chat_runner)
    return await create_and_dispatch(
        user_id,
        engine=engine,
        storage=storage,
        topic=topic,
        debaters=debaters,
        judge=judge,
        max_rounds=max_rounds,
        trigger=trigger,
    )


__all__ = [
    "configure_discussion",
    "create_and_dispatch",
    "discussion",
]
