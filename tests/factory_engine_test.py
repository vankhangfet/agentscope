# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The factory engine, over fakeredis: phases, gates, modes, recovery.

Storage is seeded directly and the engines are constructed by hand;
the walker core is driven synchronously through
``FactoryEngine._run_to_completion`` for determinism (the discussion
engine tests' discipline), and only where the behaviour IS the
background task path — approve/reject re-dispatch, the mid-run cancel
— does a test await the real dispatched task.

The fake runner extends the discussion suite's: debate turns keep the
inherited script-or-derived behaviour, and the DEVELOP turns (plan
lead, developer, reviewer) are routed by prompt content — the plan ask
says "strict JSON object", a developer prompt starts "Task ", the
reviewer's names the review — so one runner serves both engines with
no session bookkeeping.
"""
import asyncio
import json
import time
from datetime import datetime
from unittest import IsolatedAsyncioTestCase

from fastapi import HTTPException

from agentscope.app._service.discussion import DebateEngine
from agentscope.app._service.factory import FactoryEngine
from agentscope.app._service.factory import _engine as factory_module
from agentscope.app.storage import (
    DiscussionStatus,
    FactoryDecision,
    FactoryMode,
    FactoryPhase,
    FactoryStatus,
    FactoryTicketData,
    FactoryTicketRecord,
)
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.permission import PermissionMode
from discussion_engine_test import (
    USER,
    _FakeRunner,
    _model,
    _record,
    make_storage,
)

_PLAN_ASK = "strict JSON object"
"""Marker of the plan prompt: the ask the Plan Lead replies to."""

_TASK_ASK = "Task "
"""A developer prompt opens with its task header."""

_REVIEW_ASK = "Review the development"
"""Marker of the reviewer prompt."""

_GOOD_PLAN = {
    "tasks": [
        {"title": "write the module", "description": "first task"},
        {"title": "test the module", "description": "second task"},
    ],
}
"""The plan the fake Plan Lead files when nothing overrides it."""

_FALLBACK_TITLE = "Execute the plan"
"""The single task a never-parsing plan degrades to."""

_AUTO_APPROVED = "gate auto-approved (delegated mode)"
"""The decision a DELEGATED intermediate gate logs."""


class _StubWorkspaceManager(WorkspaceManagerBase):
    """An inert manager: v1 mints workspace ids without touching one."""

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ):
        """Never called by the engine in v1."""
        raise NotImplementedError

    async def close(self, workspace_id: str) -> None:
        """Never called by the engine in v1."""

    async def close_all(self) -> None:
        """Never called by the engine in v1."""


class _FactoryRunner(_FakeRunner):
    """The discussion fake, extended to serve the develop turns.

    Debate turns (debater arguments, judge verdicts, conclusions) keep
    the inherited behaviour; the three develop roles are recognized
    from what their prompt asks for and answered from the knobs below,
    so a test steers one role by setting one attribute.
    """

    def __init__(self, storage) -> None:
        super().__init__(storage)
        self.plan_replies: list[str] = []
        """One reply per plan turn, in order; the good plan default."""
        self.developer_fail_titles: set[str] = set()
        """Task titles whose developer turn raises instead of replying."""
        self.review_reply = "APPROVE: the work is sound and verified."

    def _default(self, msg):  # type: ignore[no-untyped-def]
        content = msg.get_text_content() or ""
        if _PLAN_ASK in content:
            if self.plan_replies:
                return self.plan_replies.pop(0)
            return json.dumps(_GOOD_PLAN)
        if content.startswith(_TASK_ASK):
            header = content.split("\n", 1)[0]
            title = header.split(": ", 1)[1]
            if title in self.developer_fail_titles:
                raise RuntimeError(f"developer exploded on {title}")
            return f"done: {title}"
        if _REVIEW_ASK in content:
            return self.review_reply
        return super()._default(msg)


class FactoryEngineTest(IsolatedAsyncioTestCase):
    """Phases, gates, modes, failures, cancel, recovery."""

    async def _make(self) -> tuple:
        storage = make_storage()
        runner = _FactoryRunner(storage)
        debate = DebateEngine(storage, runner)
        engine = FactoryEngine(
            storage,
            _StubWorkspaceManager(),
            runner,
            debate,
        )
        return storage, runner, debate, engine

    async def _ticket(
        self,
        storage,
        *,
        mode: FactoryMode = FactoryMode.COLLABORATIVE,
    ) -> FactoryTicketRecord:
        record = await storage.upsert_factory_ticket(
            USER,
            FactoryTicketRecord(
                user_id=USER,
                data=FactoryTicketData(
                    title="ship the widget",
                    description="The widget must ship.",
                    mode=mode,
                    chat_model_config=_model(),
                ),
            ),
        )
        return record

    async def _until_settled(
        self,
        storage,
        ticket_id: str,
        timeout: float = 5,
    ) -> FactoryTicketRecord:
        """Poll until the dispatched ticket parks or lands terminal."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = await storage.get_factory_ticket(USER, ticket_id)
            assert record is not None
            if record.data.status is not FactoryStatus.RUNNING:
                return record
            await asyncio.sleep(0.01)
        raise AssertionError("ticket never left RUNNING")

    # ------------------------------------------------------------------

    async def test_collaborative_walks_all_phases_with_gates(self) -> None:
        storage, _runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)

        await engine._run_to_completion(USER, ticket.id)

        parked = await storage.get_factory_ticket(USER, ticket.id)
        assert parked is not None
        self.assertIs(parked.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(parked.data.phase, FactoryPhase.REQUIREMENTS)
        requirements_id = parked.data.discussions[FactoryPhase.REQUIREMENTS]
        discussion = await storage.get_discussion(USER, requirements_id)
        assert discussion is not None
        self.assertIs(discussion.data.status, DiscussionStatus.COMPLETED)
        self.assertEqual(
            parked.data.requirements,
            discussion.data.conclusion,
        )
        self.assertEqual(
            [decision.phase for decision in parked.data.decisions],
            [FactoryPhase.REQUIREMENTS],
        )
        self.assertEqual(
            parked.data.decisions[0].discussion_id,
            requirements_id,
        )
        self.assertFalse(parked.data.decisions[0].note)

        # Gate 1 approved: the architecture phase is dispatched.
        advanced = await engine.approve(USER, ticket.id, "requirements ok")
        self.assertIs(advanced.data.status, FactoryStatus.RUNNING)
        self.assertIs(advanced.data.phase, FactoryPhase.ARCHITECTURE)
        parked = await self._until_settled(storage, ticket.id)
        self.assertIs(parked.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(parked.data.phase, FactoryPhase.ARCHITECTURE)
        self.assertTrue(parked.data.architecture)
        self.assertTrue(parked.data.requirements)

        # Gate 2 approved: DEVELOP runs to its own gate.
        await engine.approve(USER, ticket.id, "architecture ok")
        parked = await self._until_settled(storage, ticket.id)
        self.assertIs(parked.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(parked.data.phase, FactoryPhase.DEVELOP)
        self.assertIsNotNone(parked.data.workspace_id)
        self.assertEqual(
            [(task.title, task.status) for task in parked.data.tasks],
            [
                ("write the module", "DONE"),
                ("test the module", "DONE"),
            ],
        )
        self.assertEqual(
            [task.output for task in parked.data.tasks],
            [
                "done: write the module",
                "done: test the module",
            ],
        )
        self.assertEqual(
            parked.data.review,
            "APPROVE: the work is sound and verified.",
        )

        # The final gate completes the ticket.
        done = await engine.approve(USER, ticket.id, "ship it")
        self.assertIs(done.data.status, FactoryStatus.COMPLETED)
        self.assertIsNotNone(done.data.finished_at)
        self.assertIsNone(done.data.error)
        gate_notes = [
            (decision.phase, decision.note)
            for decision in done.data.decisions
        ]
        self.assertEqual(
            gate_notes,
            [
                (FactoryPhase.REQUIREMENTS, "requirements ok"),
                (FactoryPhase.ARCHITECTURE, "architecture ok"),
                (FactoryPhase.DEVELOP, "ship it"),
            ],
        )
        # Both phases recorded their own discussion.
        self.assertEqual(len(done.data.discussions), 2)
        ids = set(done.data.discussions.values())
        self.assertEqual(len(ids), 2)

    async def test_develop_sessions_share_the_ticket_workspace(self) -> None:
        """Plan lead, developers and reviewer each get their own session
        in the ONE per-ticket workspace, in DONT_ASK mode."""
        storage, runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)
        await engine.approve(USER, ticket.id)
        await self._until_settled(storage, ticket.id)
        await engine.approve(USER, ticket.id)
        parked = await self._until_settled(storage, ticket.id)

        def _is_develop(msg) -> bool:
            content = msg.get_text_content() or ""
            return (
                _PLAN_ASK in content
                or content.startswith(_TASK_ASK)
                or _REVIEW_ASK in content
            )

        develop_calls = [
            call for call in runner.calls if _is_develop(call[3])
        ]
        # One plan turn, one developer turn per task, one review turn.
        self.assertEqual(len(develop_calls), 4)
        names = set()
        for _user, session_id, agent_id, _msg in develop_calls:
            session = await storage.get_session(USER, agent_id, session_id)
            assert session is not None
            self.assertEqual(
                session.config.workspace_id,
                parked.data.workspace_id,
            )
            self.assertIs(
                session.state.permission_context.mode,
                PermissionMode.DONT_ASK,
            )
            names.add(session.config.name)
        self.assertEqual(
            names,
            {
                "ship the widget / plan_lead",
                "ship the widget / developer_1",
                "ship the widget / developer_2",
                "ship the widget / reviewer",
            },
        )

    async def test_delegated_auto_approves_all_but_final_gate(self) -> None:
        storage, _runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage, mode=FactoryMode.DELEGATED)

        await engine._run_to_completion(USER, ticket.id)

        parked = await storage.get_factory_ticket(USER, ticket.id)
        assert parked is not None
        # Only the final gate parked the ticket.
        self.assertIs(parked.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(parked.data.phase, FactoryPhase.DEVELOP)
        self.assertIsNotNone(parked.data.requirements)
        self.assertIsNotNone(parked.data.architecture)
        self.assertTrue(parked.data.tasks)
        auto = [
            decision
            for decision in parked.data.decisions
            if decision.conclusion == _AUTO_APPROVED
        ]
        self.assertEqual(
            [decision.phase for decision in auto],
            [FactoryPhase.REQUIREMENTS, FactoryPhase.ARCHITECTURE],
        )
        # The final gate is still human's: approve completes.
        done = await engine.approve(USER, ticket.id)
        self.assertIs(done.data.status, FactoryStatus.COMPLETED)

    async def test_failed_requirements_discussion_fails_ticket(self) -> None:
        storage, runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        runner.script = [RuntimeError("model exploded")]

        await engine._run_to_completion(USER, ticket.id)

        done = await storage.get_factory_ticket(USER, ticket.id)
        assert done is not None
        self.assertIs(done.data.status, FactoryStatus.FAILED)
        self.assertEqual(
            done.data.error,
            "requirements discussion did not complete",
        )
        self.assertIsNone(done.data.requirements)
        self.assertIsNotNone(done.data.finished_at)
        discussion = await storage.get_discussion(
            USER,
            done.data.discussions[FactoryPhase.REQUIREMENTS],
        )
        assert discussion is not None
        self.assertIs(discussion.data.status, DiscussionStatus.FAILED)

    async def test_unparsable_plan_falls_back_to_single_task(self) -> None:
        storage, runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)
        await engine.approve(USER, ticket.id)
        await self._until_settled(storage, ticket.id)
        await engine.approve(USER, ticket.id)
        runner.plan_replies = ["utter garbage", "still not json"]

        parked = await self._until_settled(storage, ticket.id)

        self.assertIs(parked.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(parked.data.phase, FactoryPhase.DEVELOP)
        self.assertEqual(len(parked.data.tasks), 1)
        task = parked.data.tasks[0]
        self.assertEqual(task.title, _FALLBACK_TITLE)
        self.assertEqual(task.description, "still not json")
        self.assertEqual(task.status, "DONE")
        # Both plan attempts ran before the fallback.
        plan_turns = [
            call
            for call in runner.calls
            if _PLAN_ASK in (call[3].get_text_content() or "")
        ]
        self.assertEqual(len(plan_turns), 2)

    async def test_failed_developer_task_fails_ticket(self) -> None:
        storage, runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)
        await engine.approve(USER, ticket.id)
        await self._until_settled(storage, ticket.id)
        await engine.approve(USER, ticket.id)
        runner.developer_fail_titles = {"test the module"}

        done = await self._until_settled(storage, ticket.id)

        self.assertIs(done.data.status, FactoryStatus.FAILED)
        assert done.data.error is not None
        self.assertIn("test the module", done.data.error)
        self.assertEqual(
            [task.status for task in done.data.tasks],
            ["DONE", "FAILED"],
        )
        # The reviewer never ran.
        self.assertIsNone(done.data.review)

    async def test_reject_at_architecture_reruns_the_phase(self) -> None:
        storage, _runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)
        await engine.approve(USER, ticket.id)
        parked = await self._until_settled(storage, ticket.id)
        assert parked.data.phase is FactoryPhase.ARCHITECTURE
        old_id = parked.data.discussions[FactoryPhase.ARCHITECTURE]

        rejected = await engine.reject(USER, ticket.id, "redo it")

        self.assertIs(rejected.data.status, FactoryStatus.RUNNING)
        self.assertIs(rejected.data.phase, FactoryPhase.ARCHITECTURE)
        self.assertIsNone(rejected.data.architecture)
        # The rejected first run's decision keeps its place, the
        # rejection logged behind it.
        self.assertEqual(
            [
                (decision.phase, decision.note)
                for decision in rejected.data.decisions
            ],
            [
                (FactoryPhase.REQUIREMENTS, ""),
                (FactoryPhase.ARCHITECTURE, ""),
                (FactoryPhase.ARCHITECTURE, "redo it"),
            ],
        )
        parked = await self._until_settled(storage, ticket.id)
        self.assertIs(parked.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(parked.data.phase, FactoryPhase.ARCHITECTURE)
        new_id = parked.data.discussions[FactoryPhase.ARCHITECTURE]
        self.assertNotEqual(new_id, old_id)
        self.assertTrue(parked.data.architecture)
        # Requirements survived the rejection untouched.
        self.assertTrue(parked.data.requirements)
        self.assertEqual(
            [decision.phase for decision in parked.data.decisions],
            [
                FactoryPhase.REQUIREMENTS,
                FactoryPhase.ARCHITECTURE,
                FactoryPhase.ARCHITECTURE,
                FactoryPhase.ARCHITECTURE,
            ],
        )

    async def test_reject_at_requirements_clears_requirements(self) -> None:
        storage, _runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)

        rejected = await engine.reject(USER, ticket.id)

        self.assertIs(rejected.data.phase, FactoryPhase.REQUIREMENTS)
        self.assertIsNone(rejected.data.requirements)
        parked = await self._until_settled(storage, ticket.id)
        self.assertIs(parked.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertTrue(parked.data.requirements)

    async def test_gate_answers_only_when_waiting(self) -> None:
        storage, runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)

        with self.assertRaises(HTTPException) as caught:
            await engine.approve(USER, "nope")
        self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(HTTPException) as caught:
            await engine.reject(USER, "nope")
        self.assertEqual(caught.exception.status_code, 404)

        # A ticket that has not reached its gate is not answerable.
        with self.assertRaises(HTTPException) as caught:
            await engine.approve(USER, ticket.id)
        self.assertEqual(caught.exception.status_code, 409)
        with self.assertRaises(HTTPException) as caught:
            await engine.reject(USER, ticket.id)
        self.assertEqual(caught.exception.status_code, 409)

        # Neither is a failed one — v1 restarts failed work as a new
        # ticket rather than resurrecting this one.
        runner.script = [RuntimeError("model exploded")]
        await engine._run_to_completion(USER, ticket.id)
        done = await storage.get_factory_ticket(USER, ticket.id)
        assert done is not None
        self.assertIs(done.data.status, FactoryStatus.FAILED)
        with self.assertRaises(HTTPException) as caught:
            await engine.approve(USER, ticket.id)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_cancel_mid_run_cancels_ticket_and_discussion(self) -> None:
        storage, runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        runner.cancel_on_call(engine, ticket.id, after_calls=0)

        await engine.dispatch(USER, ticket.id)
        for _ in range(500):
            if runner.cancelled_task is not None:
                break
            await asyncio.sleep(0.01)
        assert runner.cancelled_task is not None
        cancelled = await asyncio.wait_for(runner.cancelled_task, 5)

        self.assertIs(cancelled.data.status, FactoryStatus.CANCELLED)
        done = await storage.get_factory_ticket(USER, ticket.id)
        assert done is not None
        self.assertIs(done.data.status, FactoryStatus.CANCELLED)
        self.assertIsNotNone(done.data.finished_at)
        # The requirements discussion did not outlive the ticket.
        discussion = await storage.get_discussion(
            USER,
            done.data.discussions[FactoryPhase.REQUIREMENTS],
        )
        assert discussion is not None
        self.assertIs(discussion.data.status, DiscussionStatus.CANCELLED)

    async def test_recover_stale_fails_interrupted(self) -> None:
        storage, _runner, _debate, engine = await self._make()

        def _seed(
            status: FactoryStatus,
            phase: FactoryPhase,
        ) -> FactoryTicketRecord:
            return FactoryTicketRecord(
                user_id=USER,
                data=FactoryTicketData(
                    title="interrupted",
                    description="never finished",
                    status=status,
                    phase=phase,
                    requirements="kept artifact",
                    chat_model_config=_model(),
                ),
            )

        running = await storage.upsert_factory_ticket(
            USER,
            _seed(FactoryStatus.RUNNING, FactoryPhase.REQUIREMENTS),
        )
        waiting = await storage.upsert_factory_ticket(
            USER,
            _seed(FactoryStatus.WAITING_HUMAN, FactoryPhase.ARCHITECTURE),
        )

        touched = await engine.recover_stale()

        self.assertEqual(touched, 2)
        for ticket_id in (running.id, waiting.id):
            done = await storage.get_factory_ticket(USER, ticket_id)
            assert done is not None
            self.assertIs(done.data.status, FactoryStatus.FAILED)
            self.assertEqual(done.data.error, "interrupted by restart")
            self.assertIsNotNone(done.data.finished_at)
            # The sweep never touches the artifact already banked.
            self.assertEqual(done.data.requirements, "kept artifact")
        self.assertEqual(
            await storage.list_active_factory_tickets(),
            [],
        )

    async def test_cancel_missing_ticket_404s(self) -> None:
        _storage, _runner, _debate, engine = await self._make()
        with self.assertRaises(HTTPException) as caught:
            await engine.cancel(USER, "nope")
        self.assertEqual(caught.exception.status_code, 404)

    # ------------------------------------------------------------------
    # Gate identity (I1): an answer applies to the gate it read, only
    # ------------------------------------------------------------------

    async def test_duplicate_approve_second_gets_409(self) -> None:
        storage, _runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)

        first = await engine.approve(USER, ticket.id, "ok")

        with self.assertRaises(HTTPException) as caught:
            await engine.approve(USER, ticket.id, "again")
        self.assertEqual(caught.exception.status_code, 409)
        # The rejected duplicate changed nothing: the record is as the
        # winning answer left it.
        after = await storage.get_factory_ticket(USER, ticket.id)
        assert after is not None
        self.assertIs(after.data.status, first.data.status)
        self.assertIs(after.data.phase, first.data.phase)
        self.assertEqual(
            len(after.data.decisions),
            len(first.data.decisions),
        )

    async def test_stale_gate_token_never_applies(self) -> None:
        """The approve transition applies only to the exact gate its
        pre-check read — a record that re-parked elsewhere in between
        (another answer won the gate, the walker moved on) is left
        untouched."""
        storage, _runner, _debate, _engine = await self._make()
        ticket = await self._ticket(storage)
        await _engine._run_to_completion(USER, ticket.id)
        parked = await storage.get_factory_ticket(USER, ticket.id)
        assert parked is not None
        gate = factory_module._gate_token(parked)

        # A stale token (the walker already re-parked at the next
        # gate): the transition no-ops and reports that it never
        # applied.
        answered: list[bool] = []
        stale = parked.model_copy(deep=True)
        factory_module._apply_approve(
            "late",
            (FactoryPhase.ARCHITECTURE, len(parked.data.decisions) + 1),
            answered,
        )(stale)
        self.assertEqual(answered, [])
        self.assertIs(stale.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(stale.data.phase, FactoryPhase.REQUIREMENTS)
        self.assertEqual(stale.data.decisions[-1].note, "")

        # The fresh token applies and records the note.
        factory_module._apply_approve("on time", gate, answered)(stale)
        self.assertEqual(answered, [True])
        self.assertIs(stale.data.phase, FactoryPhase.ARCHITECTURE)
        self.assertEqual(stale.data.decisions[-1].note, "on time")

    async def test_answer_with_stale_precheck_409s(self) -> None:
        """End to end: when the record moves between an answer's
        pre-check and its transition, the answer gets a 409 and the
        new gate is untouched."""
        storage, runner, debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)

        class _StalePrecheckEngine(FactoryEngine):
            """Answers with a gate token read from a record the engine
            no longer holds — simulates the walker re-parking between
            an answer's pre-check and its transition."""

            async def _require_waiting(
                self,
                user_id: str,
                ticket_id: str,
            ) -> FactoryTicketRecord:
                record = await super()._require_waiting(
                    user_id,
                    ticket_id,
                )
                stale = record.model_copy(deep=True)
                stale.data.decisions.append(
                    FactoryDecision(
                        phase=record.data.phase,
                        discussion_id=None,
                        conclusion="a decision the record never took",
                        consensus=False,
                        created_at=datetime.now(),
                    ),
                )
                return stale

        engine = _StalePrecheckEngine(
            storage,
            _StubWorkspaceManager(),
            runner,
            debate,
        )
        with self.assertRaises(HTTPException) as caught:
            await engine.approve(USER, ticket.id, "late")
        self.assertEqual(caught.exception.status_code, 409)
        after = await storage.get_factory_ticket(USER, ticket.id)
        assert after is not None
        self.assertIs(after.data.status, FactoryStatus.WAITING_HUMAN)
        self.assertIs(after.data.phase, FactoryPhase.REQUIREMENTS)
        self.assertTrue(
            all(
                decision.note == ""
                for decision in after.data.decisions
            ),
        )

    # ------------------------------------------------------------------
    # The plan cap (I2)
    # ------------------------------------------------------------------

    async def test_plan_over_cap_truncates_and_logs(self) -> None:
        storage, runner, _debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)
        await engine.approve(USER, ticket.id)
        await self._until_settled(storage, ticket.id)
        await engine.approve(USER, ticket.id)
        runner.plan_replies = [
            json.dumps(
                {
                    "tasks": [
                        {
                            "title": f"task {index}",
                            "description": "work",
                        }
                        for index in range(1, 21)
                    ],
                },
            ),
        ]

        parked = await self._until_settled(storage, ticket.id)

        self.assertIs(parked.data.phase, FactoryPhase.DEVELOP)
        self.assertEqual(len(parked.data.tasks), 12)
        self.assertEqual(
            [task.title for task in parked.data.tasks],
            [f"task {index}" for index in range(1, 13)],
        )
        # Twelve developer turns ran — the cap, not the plan's twenty.
        developer_turns = [
            call
            for call in runner.calls
            if (call[3].get_text_content() or "").startswith(_TASK_ASK)
        ]
        self.assertEqual(len(developer_turns), 12)
        # The truncation is logged honestly.
        truncations = [
            decision
            for decision in parked.data.decisions
            if "truncated" in decision.conclusion
        ]
        self.assertEqual(len(truncations), 1)
        self.assertEqual(
            truncations[0].conclusion,
            "plan truncated from 20 to 12 tasks (cap 12)",
        )

    # ------------------------------------------------------------------
    # Settling an unsettled discussion (N5)
    # ------------------------------------------------------------------

    async def test_unsettled_discussion_fails_after_settle_wait(
        self,
    ) -> None:
        """A discussion nothing will ever settle (what a close()-torn
        debate loop leaves behind) fails its phase once the settle
        wait expires."""
        storage, _runner, debate, engine = await self._make()
        ticket = await self._ticket(storage)
        orphan = await storage.upsert_discussion(
            USER,
            _record(status=DiscussionStatus.RUNNING),
        )

        async def _stuck(
            user_id: str,
            discussion_id: str,
        ):
            return orphan

        debate.run_to_completion = _stuck
        original_wait = factory_module._SETTLE_WAIT
        factory_module._SETTLE_WAIT = 0.05
        try:
            await engine._run_to_completion(USER, ticket.id)
        finally:
            factory_module._SETTLE_WAIT = original_wait

        done = await storage.get_factory_ticket(USER, ticket.id)
        assert done is not None
        self.assertIs(done.data.status, FactoryStatus.FAILED)
        self.assertEqual(
            done.data.error,
            "requirements discussion did not complete",
        )

    async def test_hand_cancelled_discussion_cancels_ticket(self) -> None:
        """A discussion cancelled by hand (the discussions API, not the
        factory) takes the ticket down with it from the phase
        branch."""
        storage, _runner, debate, engine = await self._make()
        ticket = await self._ticket(storage)
        await engine._run_to_completion(USER, ticket.id)
        await engine.approve(USER, ticket.id)

        # Wait until the architecture discussion is on the ticket,
        # then cancel it straight through the debate engine.
        deadline = time.monotonic() + 5
        discussion_id = None
        while time.monotonic() < deadline:
            record = await storage.get_factory_ticket(USER, ticket.id)
            assert record is not None
            discussion_id = record.data.discussions.get(
                FactoryPhase.ARCHITECTURE,
            )
            if discussion_id is not None:
                break
            await asyncio.sleep(0.01)
        assert discussion_id is not None
        await debate.cancel(USER, discussion_id)

        done = await self._until_settled(storage, ticket.id)
        self.assertIs(done.data.status, FactoryStatus.CANCELLED)
        self.assertIsNotNone(done.data.finished_at)
