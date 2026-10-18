# -*- coding: utf-8 -*-
"""The software factory's engine: a ticket walker with human gates.

The FactoryEngine owns every persisted transition of a ticket the way
the debate engine owns a discussion's: each transition is persisted
before the step that depends on it, so a crash always leaves a record
the boot sweep (:meth:`FactoryEngine.recover_stale`) can classify — a
ticket still PENDING, RUNNING or WAITING_HUMAN was interrupted
mid-flight (or parked at a gate whose human never came back) and is
failed out as ``interrupted by restart``. Phase flow: REQUIREMENTS and
ARCHITECTURE each run a multi-agent discussion — created through the
discussion facade with the phase's role group, driven to completion
through the debate engine's public wrapper — whose conclusion becomes
the phase's artifact and first decision-log entry. DEVELOP mints the
per-ticket workspace, then runs the Plan Lead (task breakdown), one
Developer turn per task and a Reviewer turn on agent sessions inside
that workspace. After each phase the ticket parks at a human gate
(WAITING_HUMAN): approve advances, reject re-runs the phase from
scratch. DELEGATED mode auto-approves every gate except the final one,
which is always the human's.

The develop sessions are plain sessions under the default user origin,
in DONT_ASK mode — unattended like a debate's EXPLORE sessions, but a
Developer must write and run code in the workspace, and DONT_ASK is
the mode built for unattended work that changes things (read-only and
workspace writes allowed, every ask denied). Traceability rides the
names instead of a dedicated origin tag: agents are
``factory:<ticket>:<role>``, sessions ``<ticket> / <role>`` — v1 keeps
the session-origin union untouched rather than widen it for a reader
that does not exist yet.
"""
import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime

from fastapi import HTTPException, status

from ...._utils._common import _generate_id
from ....message import UserMsg
from ....permission import PermissionContext, PermissionMode
from ....state import AgentState
from ...discussion import create_and_dispatch
from ...storage import (
    AgentChatConfig,
    AgentData,
    AgentRecord,
    DiscussionRecord,
    DiscussionStatus,
    FactoryDecision,
    FactoryMode,
    FactoryPhase,
    FactoryStatus,
    FactoryTask,
    FactoryTicketData,
    FactoryTicketRecord,
    SessionConfig,
    StorageBase,
)
from ...workspace_manager import WorkspaceManagerBase
from .._workflow_nodes import ChatRunner
from ..discussion import DebateEngine
# The discussion judge's tolerant brace scanner — the same reading
# discipline the Plan Lead's reply gets, shared rather than duplicated.
from ..discussion._evaluation import _extract_json_object
from ..discussion._turns import read_turn_reply
from ._roles import (
    ARCHITECTURE_ROLES,
    DEVELOPER_PROMPT,
    PLAN_LEAD_PROMPT,
    REQUIREMENTS_ROLES,
    REVIEWER_PROMPT,
    ROLE_GROUP,
    to_debaters,
)

logger = logging.getLogger(__name__)

_TERMINAL = {
    FactoryStatus.COMPLETED,
    FactoryStatus.FAILED,
    FactoryStatus.CANCELLED,
}

_RESTART_ERROR = "interrupted by restart"

_DISCUSSION_TERMINAL = {
    DiscussionStatus.COMPLETED,
    DiscussionStatus.FAILED,
    DiscussionStatus.CANCELLED,
}

_SETTLE_WAIT = 2.0
"""Seconds :meth:`_resettle` waits for a racing cancel's CANCELLED
persist to land before a phase treats an unsettled discussion as its
verdict."""

_SETTLE_POLL = 0.01
"""The settle wait's poll interval."""

_MAX_DISCUSSION_ROUNDS = 2
"""Round ceiling for the phase discussions (the design's default)."""

_NO_CONCLUSION = "(no conclusion)"
"""The artifact a discussion leaves when its judge concluded nothing."""

_DEVELOPER_ROLE = "developer"
"""The logical role every task is routed to (v1: one developer)."""

_MAX_TASKS = 12
"""Ceiling on the Plan Lead's task list. The prompt asks for a short
list; this is the enforcement backstop — one unattended phase runs at
most this many sequential DONT_ASK developer sessions before the next
human gate, and the review prompt stays bounded with it."""

_PLAN_RETRY_NUDGE = (
    "\n\nYour previous reply was not the required JSON. Reply again "
    "with the JSON object only."
)

_NEXT_PHASE: dict[FactoryPhase, FactoryPhase] = {
    FactoryPhase.REQUIREMENTS: FactoryPhase.ARCHITECTURE,
    FactoryPhase.ARCHITECTURE: FactoryPhase.DEVELOP,
}
"""Where a gate's approval sends each phase; DEVELOP completes."""


class PhaseFailure(Exception):
    """One factory phase could not run; message is user-facing."""


class FactoryEngine:
    """Drives tickets: phases, gates, modes, recovery.

    One task per ticket per process — :meth:`dispatch` is idempotent
    through an atomic check-and-register with no await in between (the
    engines' shared discipline). Every read-modify-write of a ticket
    record goes through :meth:`_mutate`, which serialises transitions
    on one write lock and reloads inside it. Failed tickets are not
    answerable — v1 restarts failed work as a new ticket, keeping the
    dead one as an honest record.
    """

    def __init__(
        self,
        storage: StorageBase,
        workspace_manager: WorkspaceManagerBase,
        chat_runner: ChatRunner,
        debate_engine: DebateEngine,
    ) -> None:
        """Initialize the engine.

        Args:
            storage (`StorageBase`): Application storage.
            workspace_manager (`WorkspaceManagerBase`): The app's
                workspace manager. The develop sessions bind the
                ticket's workspace id on their config and the chat
                runner materializes the workspace through the manager
                on first use; the engine itself holds it for the
                lifecycle work later tasks attach here (v1 mints the
                id and touches nothing).
            chat_runner (`ChatRunner`): Drives one agent turn to
                completion (``ChatService.run`` in production).
            debate_engine (`DebateEngine`): Drives the phase
                discussions; the factory creates them through the
                facade and awaits them through this engine.
        """
        self._storage = storage
        self._workspace_manager = workspace_manager
        self._chat_runner = chat_runner
        self._debate_engine = debate_engine
        self._tasks: dict[str, asyncio.Task] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Dispatch and lifecycle
    # ------------------------------------------------------------------

    async def dispatch(self, user_id: str, ticket_id: str) -> None:
        """Start the ticket's walker, idempotently.

        The live-task check and the registration below have no await
        between them, so they are atomic under asyncio's cooperative
        scheduling: of two racing dispatch calls exactly one spawns a
        walker. The body never yields — the PENDING→RUNNING flip and
        the terminal pre-checks live in the walker head, where a crash
        leaves a record the sweep can classify.

        Args:
            user_id (`str`): The ticket's owner.
            ticket_id (`str`): The ticket to drive.
        """
        if self._closed:
            # A closed engine drives nothing; the next process's
            # recovery sweep picks the ticket up.
            return
        live = self._tasks.get(ticket_id)
        if live is not None and not live.done():
            return
        # create_task schedules without running the walker, so the
        # task is registered — and visible to any concurrent dispatch
        # — before this coroutine can yield.
        task = asyncio.create_task(self._loop(user_id, ticket_id))
        self._tasks[ticket_id] = task
        task.add_done_callback(
            lambda done, did=ticket_id: self._forget_task(did, done),
        )

    def _forget_task(self, ticket_id: str, task: asyncio.Task) -> None:
        """Drop the ticket's walker task once it ends, logging any
        crash that escaped the loop's own handler."""
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Factory ticket %r loop crashed.",
                ticket_id,
                exc_info=task.exception(),
            )
        if self._tasks.get(ticket_id) is task:
            self._tasks.pop(ticket_id, None)

    async def _loop(self, user_id: str, ticket_id: str) -> None:
        """Run :meth:`_run_to_completion` for one ticket, failing the
        ticket on anything that escapes it.

        The completion body already translates phase failures into
        FAILED records, so what reaches this handler is an
        engine-level fault (storage between transitions, a bug) —
        recorded as the ticket's error when it can be, logged always.
        Cancellation passes through untouched: :meth:`cancel` persists
        CANCELLED once the task settles, and a shutdown cancel
        (:meth:`close`) deliberately leaves the record mid-flight for
        the next process's sweep.
        """
        try:
            await self._run_to_completion(user_id, ticket_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Factory ticket %r loop crashed.",
                ticket_id,
                exc_info=exc,
            )
            try:
                await self._fail_ticket(user_id, ticket_id, str(exc))
            except Exception:  # pylint: disable=broad-except
                logger.error(
                    "Failing ticket %r after a loop crash failed.",
                    ticket_id,
                    exc_info=True,
                )

    async def _run_to_completion(
        self,
        user_id: str,
        ticket_id: str,
    ) -> None:
        """Walk one ticket forward until it parks at a gate or settles.

        The deterministic core the loop task wraps — tests await this
        directly instead of racing a background task. Every phase
        attempt is preceded by a fresh reload (so a cancel between
        phases is seen), and each phase handler ends in exactly one
        of: parked WAITING_HUMAN, advanced to the next phase (a
        DELEGATED gate's auto-approve), or a terminal record.

        Args:
            user_id (`str`): The ticket's owner.
            ticket_id (`str`): The ticket to drive.
        """
        record = await self._storage.get_factory_ticket(
            user_id,
            ticket_id,
        )
        if record is None:
            logger.warning(
                "Dispatch skipped: factory ticket %r not found.",
                ticket_id,
            )
            return
        if record.data.status in _TERMINAL:
            return
        if record.data.status is FactoryStatus.PENDING:
            await self._mutate(user_id, ticket_id, _mark_running)

        while True:
            record = await self._storage.get_factory_ticket(
                user_id,
                ticket_id,
            )
            if record is None or record.data.status in _TERMINAL:
                return
            if record.data.status is FactoryStatus.WAITING_HUMAN:
                # Parked at a gate; approve/reject re-dispatch.
                return
            phase = record.data.phase
            if phase is FactoryPhase.REQUIREMENTS:
                await self._run_discussion_phase(
                    user_id,
                    ticket_id,
                    FactoryPhase.REQUIREMENTS,
                    REQUIREMENTS_ROLES,
                )
            elif phase is FactoryPhase.ARCHITECTURE:
                await self._run_discussion_phase(
                    user_id,
                    ticket_id,
                    FactoryPhase.ARCHITECTURE,
                    ARCHITECTURE_ROLES,
                )
            else:
                await self._run_develop_phase(user_id, ticket_id)

    # ------------------------------------------------------------------
    # The three phases
    # ------------------------------------------------------------------

    async def _run_discussion_phase(
        self,
        user_id: str,
        ticket_id: str,
        phase: FactoryPhase,
        roles: ROLE_GROUP,
    ) -> None:
        """Run one debate phase: discussion → artifact → decision →
        gate.

        The discussion is created through the public facade (the same
        validation and judge default every creator gets) with the
        phase's role group on the ticket's model, its id recorded on
        the ticket BEFORE it is awaited — the human can follow the
        debate live from the ticket view — and then driven to
        completion. The settled record is branched on, never assumed:
        COMPLETED banks the conclusion as the phase's artifact with a
        decision logged (consensus from the rounds; a disagreement
        still decides — the gate shows it), CANCELLED cancels the
        ticket with it, and anything else (FAILED, or RUNNING after
        the debate engine was torn down mid-run) fails the phase.
        """
        record = await self._storage.get_factory_ticket(
            user_id,
            ticket_id,
        )
        if record is None:
            return
        discussion = await create_and_dispatch(
            user_id,
            engine=self._debate_engine,
            storage=self._storage,
            topic=f"{record.data.title}\n\n{record.data.description}",
            debaters=to_debaters(roles, record.data.chat_model_config),
            max_rounds=_MAX_DISCUSSION_ROUNDS,
            trigger="factory",
        )
        recorded = await self._mutate(
            user_id,
            ticket_id,
            _set_discussion(phase, discussion.id),
        )
        if recorded is None or recorded.data.status in _TERMINAL:
            return
        settled = await self._debate_engine.run_to_completion(
            user_id,
            discussion.id,
        )
        if settled.data.status not in _DISCUSSION_TERMINAL:
            settled = await self._resettle(user_id, settled)
        if settled.data.status is DiscussionStatus.COMPLETED:
            conclusion = settled.data.conclusion or _NO_CONCLUSION
            consensus = any(
                evaluation.consensus
                for evaluation in settled.data.rounds
            )
            await self._mutate(
                user_id,
                ticket_id,
                _land_artifact(
                    phase,
                    conclusion,
                    discussion.id,
                    consensus,
                ),
            )
            await self._gate(user_id, ticket_id)
        elif settled.data.status is DiscussionStatus.CANCELLED:
            # The factory's own cancel cascades through the
            # discussion; a discussion cancelled by hand takes the
            # ticket down with it — the phase died with its debate.
            await self._mutate(user_id, ticket_id, _cancel)
        else:
            await self._fail_ticket(
                user_id,
                ticket_id,
                f"{phase.value.lower()} discussion did not complete",
            )

    async def _resettle(
        self,
        user_id: str,
        settled: DiscussionRecord,
    ) -> DiscussionRecord:
        """Re-read a discussion the wrapper returned unsettled.

        ``run_to_completion`` reloads the record the instant its loop
        task settles, so a cancel's own CANCELLED persist — which that
        same cancel writes just after the task settles — can land
        after the read and leave the wrapper's answer transiently
        unsettled. Give that persist a moment before the caller takes
        the record as its verdict; still unsettled once the wait
        expires, the discussion was torn down by a ``close`` and the
        phase fails on it honestly.

        Args:
            user_id (`str`): The ticket's owner.
            settled (`DiscussionRecord`): The record as the wrapper
                returned it.

        Returns:
            `DiscussionRecord`: The freshest read — terminal whenever
                the record was going to settle at all.
        """
        record = settled
        deadline = time.monotonic() + _SETTLE_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(_SETTLE_POLL)
            fresh = await self._storage.get_discussion(
                user_id,
                settled.id,
            )
            if fresh is None:
                break
            record = fresh
            if record.data.status in _DISCUSSION_TERMINAL:
                break
        return record

    async def _run_develop_phase(
        self,
        user_id: str,
        ticket_id: str,
    ) -> None:
        """Run DEVELOP: plan → one developer per task → review → gate.

        The workspace is minted on first entry and kept for the
        ticket's life, so every develop session — and every re-run
        after a rejected gate — works in the same place. The Plan
        Lead's reply is parsed with the tolerant brace extractor and
        one retry; a plan that never parses degrades to a single task
        carrying the raw reply, so the phase always has work to do.
        Any turn failure marks its task FAILED and fails the ticket
        naming it. The gate after the review is always the human's,
        both modes.
        """
        try:
            record = await self._storage.get_factory_ticket(
                user_id,
                ticket_id,
            )
            if record is None:
                return
            if record.data.workspace_id is None:
                minted = await self._mutate(
                    user_id,
                    ticket_id,
                    _mint_workspace,
                )
                if minted is None or minted.data.status in _TERMINAL:
                    return
                record = minted

            plan_session, plan_agent = await self._ensure_develop_session(
                record,
                "plan_lead",
                PLAN_LEAD_PROMPT,
            )
            prompt = _plan_prompt(record.data)
            reply = await self._turn(
                record.user_id,
                plan_session,
                plan_agent,
                prompt,
                "plan lead",
            )
            tasks = _parse_tasks(reply)
            if tasks is None:
                # One retry in the same conversation, then give up on
                # JSON and keep the reply itself as the work.
                reply = await self._turn(
                    record.user_id,
                    plan_session,
                    plan_agent,
                    prompt + _PLAN_RETRY_NUDGE,
                    "plan lead",
                )
                tasks = _parse_tasks(reply)
            if tasks is None:
                tasks = [_fallback_task(reply)]
            parsed_count = len(tasks)
            if parsed_count > _MAX_TASKS:
                # The plan is capped, not trusted: an unbounded list
                # would run that many unattended sessions before any
                # human sees anything. The truncation is logged as a
                # decision by the transition below.
                tasks = tasks[:_MAX_TASKS]
            landed = await self._mutate(
                user_id,
                ticket_id,
                _set_tasks(tasks, parsed_count),
            )
            if landed is None or landed.data.status in _TERMINAL:
                return

            for index, task in enumerate(landed.data.tasks, start=1):
                marked = await self._mutate(
                    user_id,
                    ticket_id,
                    _task_transition(index, status="RUNNING"),
                )
                if marked is None or marked.data.status in _TERMINAL:
                    return
                session_id, agent_id = await self._ensure_develop_session(
                    marked,
                    f"developer_{index}",
                    DEVELOPER_PROMPT,
                )
                try:
                    output = await self._turn(
                        marked.user_id,
                        session_id,
                        agent_id,
                        _task_prompt(marked.data, index, task),
                        f"developer for task {task.title!r}",
                    )
                except PhaseFailure:
                    await self._mutate(
                        user_id,
                        ticket_id,
                        _task_transition(index, status="FAILED"),
                    )
                    raise
                done = await self._mutate(
                    user_id,
                    ticket_id,
                    _task_transition(index, status="DONE", output=output),
                )
                if done is None or done.data.status in _TERMINAL:
                    return

            fresh = await self._storage.get_factory_ticket(
                user_id,
                ticket_id,
            )
            if fresh is None or fresh.data.status in _TERMINAL:
                return
            review_session, review_agent = await self._ensure_develop_session(
                fresh,
                "reviewer",
                REVIEWER_PROMPT,
            )
            review = await self._turn(
                fresh.user_id,
                review_session,
                review_agent,
                _review_prompt(fresh.data),
                "reviewer",
            )
            await self._mutate(
                user_id,
                ticket_id,
                _land_review(review),
            )
            await self._gate(user_id, ticket_id)
        except asyncio.CancelledError:
            raise
        except PhaseFailure as exc:
            await self._fail_ticket(user_id, ticket_id, str(exc))

    async def _ensure_develop_session(
        self,
        record: FactoryTicketRecord,
        key: str,
        system_prompt: str,
    ) -> tuple[str, str]:
        """Mint the agent and workspace session for one develop role.

        Mirrors the discussion participant plumbing — an agent named
        after the ticket and role so the row traces back, a session
        named ``"<ticket> / <role>"`` riding the ticket's workspace
        and model — minus the in-memory cache: a develop phase invokes
        each role once per run, so there is nothing to cache (a
        rejected phase re-run mints fresh sessions; the old ones stay
        as history).

        Args:
            record (`FactoryTicketRecord`): The ticket being developed;
                its workspace must already be minted.
            key (`str`): The role slot — ``plan_lead``,
                ``developer_<index>`` or ``reviewer``.
            system_prompt (`str`): The role's instructions (the role
                catalog's development prompts).

        Returns:
            `tuple[str, str]`: The session id and agent id to drive
                turns with.

        Raises:
            `PhaseFailure`: The ticket has no workspace to develop in.
        """
        workspace_id = record.data.workspace_id
        if workspace_id is None:
            raise PhaseFailure(
                f"ticket {record.id!r} has no workspace to develop in.",
            )
        model = record.data.chat_model_config
        agent_id = await self._storage.upsert_agent(
            record.user_id,
            AgentRecord(
                user_id=record.user_id,
                data=AgentData(
                    name=f"factory:{record.id}:{key}",
                    system_prompt=system_prompt,
                    chat_config=AgentChatConfig(chat_model_config=model),
                ),
            ),
        )
        session = await self._storage.upsert_session(
            user_id=record.user_id,
            agent_id=agent_id,
            config=SessionConfig(
                workspace_id=workspace_id,
                name=f"{record.data.title} / {key}",
                chat_model_config=model,
                fallback_chat_model_config=None,
            ),
            state=AgentState(
                permission_context=PermissionContext(
                    mode=PermissionMode.DONT_ASK,
                ),
            ),
        )
        return session.id, agent_id

    async def _turn(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        prompt: str,
        who: str,
    ) -> str:
        """One develop-role turn; the phase-failure boundary.

        Everything a turn can do wrong — a chat runner that raises, a
        reply that closed with an error, a turn that parked on a tool
        call — becomes one :class:`PhaseFailure` naming the role, so
        the develop phase fails the ticket with a diagnosable error.

        Args:
            user_id (`str`): The ticket's owner.
            session_id (`str`): The role's session.
            agent_id (`str`): The role's agent.
            prompt (`str`): The turn's user message.
            who (`str`): The role, for failure messages.

        Returns:
            `str`: The reply's text content.

        Raises:
            `PhaseFailure`: The turn failed.
        """
        try:
            await self._chat_runner(
                user_id,
                session_id,
                agent_id,
                UserMsg(name="factory", content=prompt),
            )
            return await read_turn_reply(
                self._storage,
                user_id,
                session_id,
                # ``who`` stays nameless: the PhaseFailure below
                # already names the role, and the detail must not
                # repeat it (the debate engine's discipline).
                "the role",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            raise PhaseFailure(f"{who} turn failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    async def _gate(self, user_id: str, ticket_id: str) -> None:
        """Park the ticket at its phase gate, or auto-approve it.

        COLLABORATIVE tickets (and the final gate of every mode) park
        WAITING_HUMAN for :meth:`approve` / :meth:`reject`. DELEGATED
        mode auto-approves every gate except the final one: the
        auto-approval is logged as a decision and the phase advances
        with the ticket still RUNNING, so the walker rolls straight
        into the next phase.
        """
        record = await self._storage.get_factory_ticket(
            user_id,
            ticket_id,
        )
        if record is None:
            return
        auto = (
            record.data.mode is FactoryMode.DELEGATED
            and record.data.phase is not FactoryPhase.DEVELOP
        )
        if auto:
            await self._mutate(user_id, ticket_id, _auto_advance)
        else:
            await self._mutate(user_id, ticket_id, _park)

    async def approve(
        self,
        user_id: str,
        ticket_id: str,
        note: str = "",
    ) -> FactoryTicketRecord:
        """Answer a gate with approval: advance, or complete.

        REQUIREMENTS and ARCHITECTURE gates advance the ticket to the
        next phase and re-dispatch its walker; the DEVELOP gate
        completes the ticket. The note lands on the last decision —
        the one the gate is about. The answer applies only to the
        exact gate its pre-check read (a gate token); a record that
        moved on in between — another answer won the gate, or the
        walker re-parked at the next one — is left untouched.

        Args:
            user_id (`str`): The ticket's owner.
            ticket_id (`str`): The ticket waiting at its gate.
            note (`str`, optional): The human's note, logged on the
                gated decision.

        Returns:
            `FactoryTicketRecord`: The persisted record as written.

        Raises:
            `HTTPException`: 404 when the ticket is missing; 409 when
                it is not WAITING_HUMAN (only a gate is answerable —
                failed tickets restart as a new ticket in v1) or when
                the gate was answered before this answer applied.
        """
        gate = _gate_token(await self._require_waiting(user_id, ticket_id))
        answered: list[bool] = []
        persisted = await self._mutate(
            user_id,
            ticket_id,
            _apply_approve(note, gate, answered),
        )
        self._assert_answered(user_id, ticket_id, persisted, answered)
        if persisted.data.status is not FactoryStatus.COMPLETED:
            await self.dispatch(user_id, ticket_id)
        return persisted

    async def reject(
        self,
        user_id: str,
        ticket_id: str,
        note: str = "",
    ) -> FactoryTicketRecord:
        """Answer a gate with rejection: re-run the current phase.

        The phase starts over from scratch — a fresh discussion
        replaces the phase's recorded one (its artifact is cleared; a
        rejected DEVELOP loses its tasks and review, keeping the
        workspace), the rejection is logged with its note, and the
        walker is re-dispatched. Earlier phases are untouched. Like
        approve, the answer applies only to the exact gate its
        pre-check read.

        Args:
            user_id (`str`): The ticket's owner.
            ticket_id (`str`): The ticket waiting at its gate.
            note (`str`, optional): The human's note, logged on the
                rejection decision.

        Returns:
            `FactoryTicketRecord`: The persisted record as written.

        Raises:
            `HTTPException`: 404 when the ticket is missing; 409 when
                it is not WAITING_HUMAN or the gate was already
                answered.
        """
        gate = _gate_token(await self._require_waiting(user_id, ticket_id))
        answered: list[bool] = []
        persisted = await self._mutate(
            user_id,
            ticket_id,
            _apply_reject(note, gate, answered),
        )
        self._assert_answered(user_id, ticket_id, persisted, answered)
        await self.dispatch(user_id, ticket_id)
        return persisted

    async def _require_waiting(
        self,
        user_id: str,
        ticket_id: str,
    ) -> FactoryTicketRecord:
        """The gate-answer pre-check: the ticket must exist and be
        parked WAITING_HUMAN. Returns the record as read — the caller
        derives the gate token to verify inside its transition."""
        record = await self._storage.get_factory_ticket(
            user_id,
            ticket_id,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Factory ticket {ticket_id!r} not found.",
            )
        if record.data.status is not FactoryStatus.WAITING_HUMAN:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Factory ticket {ticket_id!r} is not waiting at a "
                    f"gate (status {record.data.status.value!r})."
                ),
            )
        return record

    def _assert_answered(
        self,
        user_id: str,
        ticket_id: str,
        persisted: FactoryTicketRecord | None,
        answered: list[bool],
    ) -> None:
        """Classify a gate answer's outcome from whether its
        transition applied: silent when it did, a 404 when the ticket
        vanished under it, a 409 when it did not — the record moved
        between the pre-check and the transition, so another answer
        won the gate (or the walker re-parked elsewhere) and this
        answer must not double-apply."""
        if persisted is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Factory ticket {ticket_id!r} not found.",
            )
        if not answered:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Factory ticket {ticket_id!r}'s gate was already "
                    "answered."
                ),
            )

    # ------------------------------------------------------------------
    # Cancel, close, recovery
    # ------------------------------------------------------------------

    async def cancel(
        self,
        user_id: str,
        ticket_id: str,
    ) -> FactoryTicketRecord:
        """Cancel a ticket: stop its walker, mark the record.

        The phase's live discussion is cancelled first — its CANCELLED
        record is what the awaiting walker sees (the wrapper's reload
        races this cancel's own persist, which :meth:`_resettle`
        bridges), so the walker lands the ticket's CANCELLED itself
        and no debate outlives its ticket. The walker task is settled
        after, for tickets caught mid-DEVELOP or between steps, and
        the final mutation makes CANCELLED durable either way. (A
        discussion cancelled by hand through the discussions API takes
        the ticket down from the phase branch the same way.)

        Args:
            user_id (`str`): The ticket's owner.
            ticket_id (`str`): The ticket to cancel.

        Returns:
            `FactoryTicketRecord`: The persisted, cancelled record.

        Raises:
            `HTTPException`: 404 when the ticket is missing.
        """
        record = await self._storage.get_factory_ticket(
            user_id,
            ticket_id,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Factory ticket {ticket_id!r} not found.",
            )
        discussion_id = record.data.discussions.get(record.data.phase)
        if discussion_id is not None:
            try:
                await self._debate_engine.cancel(user_id, discussion_id)
            except HTTPException as exc:
                if exc.status_code != status.HTTP_404_NOT_FOUND:
                    raise
                # The discussion was deleted under the ticket; the
                # walker's own cancellation carries the rest.
        task = self._tasks.get(ticket_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pylint: disable=broad-except
                # A walker that died of something else mid-cancellation
                # must not mask the cancel the caller asked for.
                logger.error(
                    "Factory ticket %r loop raised while being "
                    "cancelled.",
                    ticket_id,
                    exc_info=True,
                )
        # Reloaded after the walker settled: its final persist counts.
        persisted = await self._mutate(user_id, ticket_id, _cancel)
        if persisted is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Factory ticket {ticket_id!r} not found.",
            )
        return persisted

    async def close(self) -> None:
        """Stop the engine: cancel every live ticket walker.

        Storage must never close underneath a running walker, so the
        lifespan calls this before tearing the stack down. Records are
        deliberately left as they are — a ticket mid-flight stays
        RUNNING (or WAITING_HUMAN at its gate), exactly what a crashed
        process leaves behind and what the next process's
        :meth:`recover_stale` fails out.
        """
        self._closed = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def recover_stale(self) -> int:
        """Fail out tickets a previous process left un-settled.

        Every still-active ticket (PENDING, RUNNING or WAITING_HUMAN —
        a parked gate's human may never return) belonged to a process
        that died — this engine's task registry is empty at boot — and
        is marked FAILED with ``interrupted by restart``, artifacts
        already banked left untouched. A v1 ticket is not resumed: its
        develop sessions are process-local and a half-replayed phase
        would lie, so an honest failure beats it. Each record is
        reconciled in isolation: one unreadable or failing record is
        logged and skipped, never aborting the sweep (or the boot that
        runs it).

        Returns:
            `int`: How many tickets the sweep rewrote.
        """
        touched = 0
        for record in await self._storage.list_active_factory_tickets():
            live = self._tasks.get(record.id)
            if live is not None and not live.done():
                # This process is driving it; not stale.
                continue
            try:
                persisted = await self._mutate(
                    record.user_id,
                    record.id,
                    _interrupted,
                )
            except Exception:  # pylint: disable=broad-except
                logger.error(
                    "Recovering factory ticket %r failed; skipping it.",
                    record.id,
                    exc_info=True,
                )
                continue
            if persisted is not None:
                logger.warning(
                    "Factory ticket %r was interrupted by a restart; "
                    "failed out.",
                    record.id,
                )
                touched += 1
        return touched

    # ------------------------------------------------------------------
    # Transition plumbing
    # ------------------------------------------------------------------

    async def _fail_ticket(
        self,
        user_id: str,
        ticket_id: str,
        error: str,
    ) -> None:
        """Persist one FAILED transition, terminal statuses kept."""
        persisted = await self._mutate(
            user_id,
            ticket_id,
            _fail_transition(error),
        )
        if persisted is None:
            logger.warning(
                "Failing ticket %r found it gone.",
                ticket_id,
            )

    async def _mutate(
        self,
        user_id: str,
        ticket_id: str,
        apply: Callable[[FactoryTicketRecord], None],
    ) -> FactoryTicketRecord | None:
        """One persisted ticket transition, serialised on the write
        lock.

        The record is reloaded inside the lock so a transition never
        overwrites a concurrent write (a cancel racing a phase
        persist); *apply* mutates the fresh record in place and the
        persist happens before anything reads it back. Every apply is
        guarded — walker-internal transitions fire only while the
        walker owns the record (RUNNING), settle transitions keep
        terminal statuses — so a transition aimed at a ticket that
        moved underneath it steps aside.

        Args:
            user_id (`str`): The ticket's owner.
            ticket_id (`str`): The ticket to transition.
            apply (`Callable`): The mutation, run under the lock.

        Returns:
            `FactoryTicketRecord | None`: The persisted record, or None
                when the ticket no longer exists.
        """
        async with self._write_lock:
            record = await self._storage.get_factory_ticket(
                user_id,
                ticket_id,
            )
            if record is None:
                return None
            apply(record)
            return await self._storage.upsert_factory_ticket(
                user_id,
                record,
            )


# ----------------------------------------------------------------------
# Transition helpers
# ----------------------------------------------------------------------


def _walker_owns(record: FactoryTicketRecord) -> bool:
    """Whether the walker still owns the record: RUNNING — not yet
    started, parked at a gate or already settled."""
    return record.data.status is FactoryStatus.RUNNING


def _mark_running(record: FactoryTicketRecord) -> None:
    """Transition helper: PENDING → RUNNING.

    Guarded so a cancel that lands between the walker's read and this
    transition is never resurrected — the mutation reloads inside the
    write lock and the fresh status decides."""
    if record.data.status is not FactoryStatus.PENDING:
        return
    record.data.status = FactoryStatus.RUNNING


def _set_discussion(
    phase: FactoryPhase,
    discussion_id: str,
) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: record the phase's discussion id."""

    def set_discussion(record: FactoryTicketRecord) -> None:
        if not _walker_owns(record):
            return
        record.data.discussions[phase] = discussion_id

    return set_discussion


def _land_artifact(
    phase: FactoryPhase,
    conclusion: str,
    discussion_id: str,
    consensus: bool,
) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: bank a debate phase's artifact and
    log its decision — the first entry the phase's gate is about."""

    def land(record: FactoryTicketRecord) -> None:
        if not _walker_owns(record):
            return
        if phase is FactoryPhase.REQUIREMENTS:
            record.data.requirements = conclusion
        else:
            record.data.architecture = conclusion
        record.data.decisions.append(
            FactoryDecision(
                phase=phase,
                discussion_id=discussion_id,
                conclusion=conclusion,
                consensus=consensus,
                created_at=datetime.now(),
            ),
        )

    return land


def _auto_advance(record: FactoryTicketRecord) -> None:
    """Transition helper: a DELEGATED gate's auto-approval.

    The approval is logged as a decision and the phase advances with
    the ticket still RUNNING, so the walker rolls straight on. Only
    ever applied to the two intermediate gates — the final one is
    always the human's.
    """
    if not _walker_owns(record):
        return
    record.data.decisions.append(
        FactoryDecision(
            phase=record.data.phase,
            discussion_id=record.data.discussions.get(
                record.data.phase,
            ),
            conclusion="gate auto-approved (delegated mode)",
            consensus=False,
            created_at=datetime.now(),
        ),
    )
    record.data.phase = _NEXT_PHASE[record.data.phase]


def _park(record: FactoryTicketRecord) -> None:
    """Transition helper: WAITING_HUMAN at the current gate."""
    if record.data.status in _TERMINAL:
        return
    record.data.status = FactoryStatus.WAITING_HUMAN


def _gate_token(record: FactoryTicketRecord) -> tuple[FactoryPhase, int]:
    """The identity of the gate a WAITING_HUMAN record is parked at:
    its phase plus how many decisions have landed.

    Every way a gate resolves or a phase re-runs grows the decision
    list or moves the phase, so two different parks never share a
    token — which is what lets an answer's transition verify it is
    applying to the very gate its pre-check read, not to whichever
    park happens to hold WAITING_HUMAN by the time the write lock is
    taken.
    """
    return (record.data.phase, len(record.data.decisions))


def _apply_approve(
    note: str,
    gate: tuple[FactoryPhase, int],
    answered: list[bool],
) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: a gate's approval.

    Fires only when the record still parks at *gate* (WAITING_HUMAN
    and the same token the answer's pre-check read): the two
    intermediate gates advance the phase and hand the record back to
    the walker (the caller re-dispatches), the final gate completes
    the ticket. The note lands on the last decision — the one this
    gate is about. A no-op records nothing in *answered*, which is how
    the caller learns it lost the gate.
    """

    def apply(record: FactoryTicketRecord) -> None:
        if record.data.status is not FactoryStatus.WAITING_HUMAN:
            return
        if _gate_token(record) != gate:
            return
        answered.append(True)
        phase = record.data.phase
        if phase in _NEXT_PHASE:
            record.data.phase = _NEXT_PHASE[phase]
            record.data.status = FactoryStatus.RUNNING
        else:
            record.data.status = FactoryStatus.COMPLETED
            record.data.finished_at = datetime.now()
        if note and record.data.decisions:
            record.data.decisions[-1].note = note

    return apply


def _apply_reject(
    note: str,
    gate: tuple[FactoryPhase, int],
    answered: list[bool],
) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: a gate's rejection.

    Fires only when the record still parks at *gate* (WAITING_HUMAN
    and the same token the answer's pre-check read): the rejection is
    logged with its note, the phase's own output is cleared (its
    discussion is replaced when the re-run records its own id; a
    rejected DEVELOP loses its tasks and review but keeps the
    workspace), and the record goes back to the walker. A no-op
    records nothing in *answered*.
    """

    def apply(record: FactoryTicketRecord) -> None:
        if record.data.status is not FactoryStatus.WAITING_HUMAN:
            return
        if _gate_token(record) != gate:
            return
        answered.append(True)
        phase = record.data.phase
        record.data.decisions.append(
            FactoryDecision(
                phase=phase,
                discussion_id=record.data.discussions.get(phase),
                conclusion=f"gate rejected: re-running {phase.value}",
                note=note,
                consensus=False,
                created_at=datetime.now(),
            ),
        )
        if phase is FactoryPhase.DEVELOP:
            record.data.tasks = []
            record.data.review = None
        elif phase is FactoryPhase.REQUIREMENTS:
            record.data.requirements = None
        else:
            record.data.architecture = None
        record.data.status = FactoryStatus.RUNNING

    return apply


def _mint_workspace(record: FactoryTicketRecord) -> None:
    """Transition helper: the per-ticket workspace, minted once on
    DEVELOP's first entry and kept for the ticket's life."""
    if not _walker_owns(record):
        return
    if record.data.workspace_id is None:
        record.data.workspace_id = _generate_id()


def _set_tasks(
    tasks: list[FactoryTask],
    parsed_count: int | None = None,
) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: the Plan Lead's breakdown lands as
    the ticket's task list, all PENDING.

    *parsed_count* is how many tasks the plan actually held before the
    cap truncated it; when fewer tasks land than were parsed, the
    truncation is logged as a DEVELOP decision rather than passed
    over silently.
    """
    count = parsed_count if parsed_count is not None else len(tasks)

    def set_tasks(record: FactoryTicketRecord) -> None:
        if not _walker_owns(record):
            return
        record.data.tasks = [task.model_copy() for task in tasks]
        if len(tasks) < count:
            record.data.decisions.append(
                FactoryDecision(
                    phase=FactoryPhase.DEVELOP,
                    discussion_id=None,
                    conclusion=(
                        f"plan truncated from {count} to {len(tasks)} "
                        f"tasks (cap {_MAX_TASKS})"
                    ),
                    consensus=True,
                    created_at=datetime.now(),
                ),
            )

    return set_tasks


def _task_transition(
    index: int,
    status: str,
    output: str | None = None,
) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: one task's own lifecycle step.

    ``index`` is 1-based into the ticket's task list — the same index
    that names the task's developer session, so a task and its
    conversation stay paired across the run.
    """

    def transition(record: FactoryTicketRecord) -> None:
        if not _walker_owns(record):
            return
        if len(record.data.tasks) < index:
            return
        task = record.data.tasks[index - 1]
        task.status = status
        if output is not None:
            task.output = output

    return transition


def _land_review(
    review: str,
) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: the reviewer's output as the review
    artifact and the decision the final gate is about."""

    def land(record: FactoryTicketRecord) -> None:
        if not _walker_owns(record):
            return
        record.data.review = review
        record.data.decisions.append(
            FactoryDecision(
                phase=FactoryPhase.DEVELOP,
                discussion_id=None,
                conclusion=review,
                consensus=False,
                created_at=datetime.now(),
            ),
        )

    return land


def _cancel(record: FactoryTicketRecord) -> None:
    """Transition helper: CANCELLED, terminal statuses kept."""
    if record.data.status in _TERMINAL:
        return
    record.data.status = FactoryStatus.CANCELLED
    record.data.finished_at = datetime.now()


def _fail_transition(error: str) -> Callable[[FactoryTicketRecord], None]:
    """Transition helper factory: FAILED with the error."""

    def fail(record: FactoryTicketRecord) -> None:
        if record.data.status in _TERMINAL:
            return
        record.data.status = FactoryStatus.FAILED
        record.data.error = error
        record.data.finished_at = datetime.now()

    return fail


def _interrupted(record: FactoryTicketRecord) -> None:
    """Transition helper: the stale sweep's FAILED, with its cause."""
    if record.data.status in _TERMINAL:
        return
    record.data.status = FactoryStatus.FAILED
    record.data.error = _RESTART_ERROR
    record.data.finished_at = datetime.now()


# ----------------------------------------------------------------------
# Prompt rendering and plan parsing
# ----------------------------------------------------------------------


def _plan_prompt(data: FactoryTicketData) -> str:
    """The Plan Lead's ask: the ticket with its approved artifacts."""
    lines = [
        f"Ticket: {data.title}",
        "",
        "The problem statement:",
        data.description,
        "",
        "Approved requirements:",
        data.requirements or "(none)",
        "",
        "Approved architecture:",
        data.architecture or "(none)",
        "",
        "Break the work down now. Reply with ONE strict JSON object "
        'and nothing else, of the shape {"tasks": [{"title": "...", '
        '"description": "..."}]}. Keep the list to at most '
        f"{_MAX_TASKS} tasks.",
    ]
    return "\n".join(lines)


def _task_prompt(
    data: FactoryTicketData,
    index: int,
    task: FactoryTask,
) -> str:
    """One developer's ask: exactly one task, by its number."""
    return (
        f"Task {index} of {len(data.tasks)}: {task.title}\n"
        f"{task.description}\n\n"
        "Implement this one task in the workspace, then report what "
        "changed and how it was verified."
    )


def _review_prompt(data: FactoryTicketData) -> str:
    """The reviewer's ask: every task with what its developer
    reported."""
    lines = [
        f"Review the development done for ticket {data.title!r}.",
        "",
        "The tasks and their developers' reports:",
    ]
    for index, task in enumerate(data.tasks, start=1):
        lines.append(f"- [{index}] {task.title}: {task.output}")
    lines.append("")
    lines.append("Review the work now.")
    return "\n".join(lines)


def _parse_tasks(reply: str) -> list[FactoryTask] | None:
    """Parse the Plan Lead's reply into the task list, or None.

    Same tolerant reading as the judge's verdict: the first balanced
    JSON object is scanned out of the reply, and anything that is not
    exactly the required shape — ``tasks`` a list of entries each
    carrying a usable title — is a parse failure (the caller retries
    once, then falls back to a single task). Entries with unusable
    titles are skipped, not fatal: one malformed bullet must not
    bury the tasks around it.
    """
    candidate = _extract_json_object(reply)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("tasks")
    if not isinstance(raw, list):
        return None
    tasks: list[FactoryTask] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        description = entry.get("description")
        tasks.append(
            FactoryTask(
                title=title,
                description=(
                    description if isinstance(description, str) else ""
                ),
                assignee=_DEVELOPER_ROLE,
            ),
        )
    return tasks or None


def _fallback_task(reply: str) -> FactoryTask:
    """The plan that never parsed becomes one task carrying it raw:
    the developer works from the Plan Lead's own words."""
    return FactoryTask(
        title="Execute the plan",
        description=reply,
        assignee=_DEVELOPER_ROLE,
    )
