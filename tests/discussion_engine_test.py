# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The discussion engines and facade, over fakeredis.

Storage is seeded directly and the engines are constructed by hand;
the debate core is driven synchronously through
``DebateEngine._run_to_completion`` for determinism, exactly like the
workflow engine tests drive loop semantics without racing a background
task. Only the cancel and facade tests exercise the real task path,
because that is what those behaviours are.
"""
import asyncio
import json
import re
import time
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi import HTTPException

from agentscope.app._service.discussion import DebateEngine
from agentscope.app.discussion import (
    configure_discussion,
    discussion,
)
from agentscope.app.storage import (
    ChatModelConfig,
    DebaterConfig,
    DiscussionData,
    DiscussionOrigin,
    DiscussionRecord,
    DiscussionStatus,
    JudgeConfig,
    RedisStorage,
)
from agentscope.message import AssistantMsg, Msg

USER = "user-1"

_TERMINAL = (
    DiscussionStatus.COMPLETED,
    DiscussionStatus.FAILED,
    DiscussionStatus.CANCELLED,
)


def make_storage() -> RedisStorage:
    """A RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def _model(model: str = "m") -> ChatModelConfig:
    """A minimal chat model config."""
    return ChatModelConfig(
        type="openai_credential",
        credential_id="c-1",
        model=model,
        parameters={},
    )


def _debater(name: str) -> DebaterConfig:
    """One debater with a stance."""
    return DebaterConfig(
        name=name,
        system_prompt=f"You argue for {name}.",
        chat_model_config=_model(),
    )


def _record(
    *,
    names: tuple[str, ...] = ("pro", "con"),
    max_rounds: int = 2,
    status: DiscussionStatus = DiscussionStatus.PENDING,
) -> DiscussionRecord:
    """A PENDING discussion record ready to dispatch."""
    return DiscussionRecord(
        user_id=USER,
        data=DiscussionData(
            topic="cats vs dogs",
            max_rounds=max_rounds,
            debaters=[_debater(name) for name in names],
            judge=JudgeConfig(chat_model_config=_model()),
            status=status,
        ),
    )


def judge_json(
    names: tuple[str, ...] = ("pro", "con"),
    *,
    consensus: bool = False,
) -> str:
    """A strict-JSON verdict over *names*, alternating strengths."""
    assessments = [
        {
            "debater": name,
            "strength": 4 if index % 2 == 0 else 3,
            "rebutted": index % 2 == 1,
            "note": f"note {index}",
        }
        for index, name in enumerate(names)
    ]
    return json.dumps(
        {
            "assessments": assessments,
            "consensus": consensus,
            "summary": "the round in one line",
        },
    )


class _FakeRunner:
    """One canned reply per turn, in turn order.

    The incoming user message is persisted before the reply — the page
    shape prod ``ChatService.run`` leaves behind. ``script`` entries
    are consumed one per turn, in order; once the script runs out the
    runner derives a reply from the prompt: a verdict JSON when the
    judge is asked for one (assessing the debater names it finds in
    the prompt), a conclusion when the conclusion is asked for, an
    argument otherwise. A ``BaseException`` entry is raised instead of
    filed, standing in for a runner that blows up.
    """

    VERDICT_ASK = "Reply with ONE JSON object"

    def __init__(self, storage: RedisStorage) -> None:
        self._storage = storage
        self.script: list[str | BaseException] = []
        self.calls: list[tuple[str, str, str, Msg]] = []
        # Set to schedule engine.cancel(...) as a task once enough
        # turns have run — how the mid-run cancel test times itself.
        self._cancel_after: int | None = None
        self._cancel_engine: DebateEngine | None = None
        self._cancel_id: str | None = None
        self.cancelled_task: asyncio.Task | None = None

    def cancel_on_call(
        self,
        engine: DebateEngine,
        discussion_id: str,
        after_calls: int = 0,
    ) -> None:
        """Cancel the discussion once *after_calls* turns have run."""
        self._cancel_after = after_calls
        self._cancel_engine = engine
        self._cancel_id = discussion_id

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        self.calls.append((user_id, session_id, agent_id, msg))
        if (
            self.cancelled_task is None
            and self._cancel_after is not None
            and len(self.calls) > self._cancel_after
        ):
            assert self._cancel_engine is not None
            self.cancelled_task = asyncio.create_task(
                self._cancel_engine.cancel(USER, self._cancel_id),
            )
        await self._storage.upsert_message(user_id, session_id, msg)
        reply: Any = (
            self.script.pop(0) if self.script else self._default(msg)
        )
        if isinstance(reply, BaseException):
            raise reply
        await self._storage.upsert_message(
            USER,
            session_id,
            AssistantMsg(name=agent_id, content=reply),
        )

    def _default(self, msg: Msg) -> str:
        """A reply derived from what the prompt asks for."""
        content = msg.get_text_content() or ""
        if self.VERDICT_ASK in content:
            names = tuple(
                re.findall(r"^\[(.+?)\] ", content, flags=re.MULTILINE),
            )
            return judge_json(names)
        if "final conclusion" in content:
            return "Both sides met in the middle; the debate is settled."
        return f"argument {len(self.calls)}"


async def _drive(
    record: DiscussionRecord,
    runner: _FakeRunner,
) -> DiscussionRecord:
    """Persist *record* and drive it to completion synchronously."""
    storage = runner._storage
    saved = await storage.upsert_discussion(USER, record)
    engine = DebateEngine(storage, runner)
    await engine._run_to_completion(USER, saved.id)
    done = await storage.get_discussion(USER, saved.id)
    assert done is not None
    return done


class DebateEngineTest(IsolatedAsyncioTestCase):
    """Rounds, stop conditions, failures, cancel, recovery."""

    async def _make(self) -> tuple[RedisStorage, _FakeRunner]:
        storage = make_storage()
        return storage, _FakeRunner(storage)

    async def test_happy_two_debaters_two_rounds(self) -> None:
        storage, runner = await self._make()
        done = await _drive(_record(max_rounds=2), runner)

        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)
        self.assertIsNotNone(done.data.finished_at)
        self.assertIsNone(done.data.error)
        self.assertEqual(len(done.data.rounds), 2)
        for evaluation in done.data.rounds:
            self.assertEqual(
                [argument.debater for argument in evaluation.arguments],
                ["pro", "con"],
            )
            self.assertEqual(
                [argument.strength for argument in evaluation.arguments],
                [4, 3],
            )
            self.assertEqual(
                [argument.rebutted for argument in evaluation.arguments],
                [False, True],
            )
            self.assertEqual(
                [argument.note for argument in evaluation.arguments],
                ["note 0", "note 1"],
            )
            self.assertFalse(evaluation.consensus)
            self.assertEqual(evaluation.summary, "the round in one line")
        self.assertTrue(done.data.conclusion)

        # Judge + debater sessions exist with a DiscussionOrigin naming
        # the discussion, and each debater's rounds accumulated in ONE
        # conversation: pro 2 turns, con 2 turns, judge 2 verdicts + 1
        # conclusion.
        sessions = {
            session_id: [
                call for call in runner.calls if call[1] == session_id
            ]
            for _user, session_id, _agent, _msg in runner.calls
        }
        self.assertEqual(
            sorted(len(turns) for turns in sessions.values()),
            [2, 2, 3],
        )
        origins: dict[str, DiscussionOrigin] = {}
        for _user, session_id, agent_id, _msg in runner.calls:
            for session in await storage.list_sessions(USER, agent_id):
                if session.id != session_id:
                    continue
                origin = session.origin
                assert isinstance(origin, DiscussionOrigin)
                origins[origin.debater] = origin
        self.assertEqual(set(origins), {"pro", "con", "judge"})
        for origin in origins.values():
            self.assertEqual(origin.discussion_id, done.id)

    async def test_consensus_stops_after_first_round(self) -> None:
        _storage, runner = await self._make()
        runner.script = [
            "argument pro 1",
            "argument con 1",
            judge_json(consensus=True),
        ]

        done = await _drive(_record(max_rounds=2), runner)

        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)
        self.assertEqual(len(done.data.rounds), 1)
        self.assertTrue(done.data.rounds[0].consensus)
        self.assertTrue(done.data.conclusion)

    async def test_max_rounds_one(self) -> None:
        _storage, runner = await self._make()

        done = await _drive(_record(max_rounds=1), runner)

        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)
        self.assertEqual(len(done.data.rounds), 1)
        self.assertTrue(done.data.conclusion)
        # 2 debaters + 1 verdict + 1 conclusion — no second round.
        self.assertEqual(len(runner.calls), 4)

    async def test_judge_parse_failure_degrades_round(self) -> None:
        _storage, runner = await self._make()
        runner.script = [
            "argument pro 1",
            "argument con 1",
            "I refuse to answer in JSON.",
            "still refusing",
        ]

        done = await _drive(_record(max_rounds=1), runner)

        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)
        evaluation = done.data.rounds[0]
        for argument in evaluation.arguments:
            self.assertIsNone(argument.strength)
            self.assertIsNone(argument.rebutted)
            self.assertEqual(argument.note, "")
        self.assertFalse(evaluation.consensus)
        self.assertIn("judge unavailable", evaluation.summary)
        # The conclusion still ran — the debate survives its judge.
        self.assertTrue(done.data.conclusion)
        # Both judge attempts happened (the retry fired once).
        verdict_turns = [
            call for call in runner.calls
            if _FakeRunner.VERDICT_ASK in (call[3].get_text_content() or "")
        ]
        self.assertEqual(len(verdict_turns), 2)

    async def test_judge_turn_raises_then_recovers(self) -> None:
        _storage, runner = await self._make()
        # The judge's first verdict turn blows up; the retry gets the
        # default verdict and the round is still evaluated.
        runner.script = [
            "argument pro 1",
            "argument con 1",
            RuntimeError("judge runner exploded"),
        ]

        done = await _drive(_record(max_rounds=1), runner)

        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)
        evaluation = done.data.rounds[0]
        self.assertEqual(
            [argument.strength for argument in evaluation.arguments],
            [4, 3],
        )
        self.assertEqual(evaluation.summary, "the round in one line")

    async def test_judge_json_wrapped_in_prose_still_parses(self) -> None:
        _storage, runner = await self._make()
        runner.script = [
            "argument pro 1",
            "argument con 1",
            "Sure, here is my assessment:\n```json\n"
            + judge_json()
            + "\n```\nHope that helps.",
        ]

        done = await _drive(_record(max_rounds=1), runner)

        evaluation = done.data.rounds[0]
        self.assertEqual(
            [argument.strength for argument in evaluation.arguments],
            [4, 3],
        )
        self.assertEqual(evaluation.summary, "the round in one line")

    async def test_debater_failure_fails_discussion(self) -> None:
        _storage, runner = await self._make()
        runner.script = [RuntimeError("model exploded")]

        done = await _drive(_record(max_rounds=2), runner)

        self.assertIs(done.data.status, DiscussionStatus.FAILED)
        assert done.data.error is not None
        self.assertIn("round 1", done.data.error)
        self.assertIn("pro", done.data.error)
        self.assertIn("model exploded", done.data.error)
        self.assertEqual(done.data.rounds, [])
        self.assertIsNone(done.data.conclusion)

    async def test_cancel_mid_run(self) -> None:
        storage, runner = await self._make()
        saved = await storage.upsert_discussion(USER, _record())
        engine = DebateEngine(storage, runner)
        runner.cancel_on_call(engine, saved.id, after_calls=0)

        await engine.dispatch(USER, saved.id)
        # Zero-await dispatch: the loop task starts on the next tick,
        # and its first debater turn is what schedules the cancel.
        for _ in range(500):
            if runner.cancelled_task is not None:
                break
            await asyncio.sleep(0.01)
        assert runner.cancelled_task is not None
        cancelled = await asyncio.wait_for(runner.cancelled_task, 5)

        self.assertIs(cancelled.data.status, DiscussionStatus.CANCELLED)
        done = await storage.get_discussion(USER, saved.id)
        assert done is not None
        self.assertIs(done.data.status, DiscussionStatus.CANCELLED)
        self.assertEqual(done.data.rounds, [])
        # The turn in flight when the cancel landed was the last one.
        self.assertEqual(len(runner.calls), 1)

    async def test_cancel_missing_discussion_404s(self) -> None:
        storage, runner = await self._make()
        engine = DebateEngine(storage, runner)
        with self.assertRaises(HTTPException) as caught:
            await engine.cancel(USER, "nope")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_recover_stale_fails_interrupted(self) -> None:
        storage, runner = await self._make()
        running = await storage.upsert_discussion(
            USER,
            _record(status=DiscussionStatus.RUNNING),
        )
        pending = await storage.upsert_discussion(
            USER,
            _record(status=DiscussionStatus.PENDING),
        )

        engine = DebateEngine(storage, runner)
        touched = await engine.recover_stale()

        self.assertEqual(touched, 2)
        for discussion_id in (running.id, pending.id):
            done = await storage.get_discussion(USER, discussion_id)
            assert done is not None
            self.assertIs(done.data.status, DiscussionStatus.FAILED)
            self.assertEqual(done.data.error, "interrupted by restart")
            self.assertIsNotNone(done.data.finished_at)
        # Terminal now — the active index no longer lists them.
        self.assertEqual(await storage.list_active_discussions(), [])

    async def test_recover_stale_skips_live_task(self) -> None:
        storage, runner = await self._make()
        saved = await storage.upsert_discussion(
            USER,
            _record(status=DiscussionStatus.RUNNING),
        )
        engine = DebateEngine(storage, runner)
        # A live loop task registered under the discussion id: the
        # sweep must not touch a discussion this process is driving.
        engine._tasks[saved.id] = asyncio.create_task(asyncio.sleep(3600))
        try:
            touched = await engine.recover_stale()
        finally:
            engine._tasks[saved.id].cancel()
        self.assertEqual(touched, 0)
        done = await storage.get_discussion(USER, saved.id)
        assert done is not None
        self.assertIs(done.data.status, DiscussionStatus.RUNNING)


class FacadeTest(IsolatedAsyncioTestCase):
    """The public facade: validation, persist, dispatch."""

    def setUp(self) -> None:
        configure_discussion(None)

    def tearDown(self) -> None:
        configure_discussion(None)

    async def test_facade_persists_pending_and_dispatches(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)
        engine = DebateEngine(storage, runner)
        configure_discussion(engine)

        record = await discussion(
            USER,
            "cats vs dogs",
            debaters=[_debater("pro"), _debater("con")],
            max_rounds=1,
            storage=storage,
            chat_runner=runner,
        )

        # The facade's contract: PENDING, durable, before dispatch.
        self.assertIs(record.data.status, DiscussionStatus.PENDING)
        self.assertEqual(record.data.trigger, "manual")
        persisted = await storage.get_discussion(USER, record.id)
        self.assertIsNotNone(persisted)
        # Dispatch happened: a live loop task drove the record on.
        task = engine._tasks.get(record.id)
        self.assertIsNotNone(task)
        await asyncio.wait_for(task, 5)
        done = await storage.get_discussion(USER, record.id)
        assert done is not None
        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)

    async def test_facade_without_registered_engine_still_runs(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)

        record = await discussion(
            USER,
            "cats vs dogs",
            debaters=[_debater("solo")],
            max_rounds=1,
            storage=storage,
            chat_runner=runner,
            trigger="chat_tool",
        )

        done = await storage.get_discussion(USER, record.id)
        assert done is not None
        self.assertEqual(done.data.trigger, "chat_tool")
        await _poll_terminal(storage, record.id)

    async def test_too_many_debaters_rejected(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)
        with self.assertRaises(ValueError) as caught:
            await discussion(
                USER,
                "cats vs dogs",
                debaters=[_debater(f"d{i}") for i in range(7)],
                storage=storage,
                chat_runner=runner,
            )
        self.assertIn("1 to 6", str(caught.exception))

    async def test_no_debaters_rejected(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)
        with self.assertRaises(ValueError):
            await discussion(
                USER,
                "cats vs dogs",
                debaters=[],
                storage=storage,
                chat_runner=runner,
            )

    async def test_empty_topic_rejected(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)
        with self.assertRaises(ValueError) as caught:
            await discussion(
                USER,
                "   ",
                debaters=[_debater("pro")],
                storage=storage,
                chat_runner=runner,
            )
        self.assertIn("non-empty", str(caught.exception))

    async def test_duplicate_debater_names_rejected(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)
        with self.assertRaises(ValueError) as caught:
            await discussion(
                USER,
                "cats vs dogs",
                debaters=[_debater("pro"), _debater("pro")],
                storage=storage,
                chat_runner=runner,
            )
        self.assertIn("unique", str(caught.exception))


async def _poll_terminal(
    storage: RedisStorage,
    discussion_id: str,
    timeout: float = 5,
) -> DiscussionRecord:
    """Poll a record until terminal; a one-shot engine's loop task
    drives it in the background."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = await storage.get_discussion(USER, discussion_id)
        assert record is not None
        if record.data.status in _TERMINAL:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("discussion never reached a terminal state")
