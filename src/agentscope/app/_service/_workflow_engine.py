# -*- coding: utf-8 -*-
"""The workflow run engine: dispatch, retries, park/resume, recovery.

The engine owns every persisted transition of a run. Handlers do one
attempt in memory; the engine records the attempt, enforces retry
budgets, parks on approvals and resumes runs a crashed process left
mid-flight. Each transition is persisted before the step that depends
on it, so a crash always leaves a run :meth:`recover_stale_runs` can
classify: a node stuck RUNNING was an interrupted attempt, a node
WAITING_HUMAN is a park to re-derive, and everything else is either
still walkable or already terminal.

Retries are not selective about failure causes: every
:class:`NodeFailure` counts against the failing node type's budget.
The retryable-exception classes of spec §5 never come into play, as
``ChatService`` folds model errors into error-bearing replies instead
of raising, so a failed model call reaches the engine as a plain
:class:`NodeFailure` like any other handler fault.
"""
import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

from ..message_bus import MessageBus
from ..storage import (
    AgentData,
    AgentNodeConfig,
    AgentRecord,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    NodeRunState,
    NotifyNodeConfig,
    RunStatus,
    StorageBase,
    WorkflowNode,
    WorkflowRunRecord,
)
from ._workflow_nodes import (
    _SESSIONS_KEY,
    ApprovalPending,
    ChatRunner,
    JudgeAgentFactory,
    NodeFailure,
    NodeHandlers,
)
from ._workflow_planner import ready_nodes, run_is_finished

if TYPE_CHECKING:
    from ..channel import ChannelClients

logger = logging.getLogger(__name__)

_RUN_TERMINAL = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}

_NODE_TERMINAL = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}

_VERDICTS = ("approve", "reject")


class WorkflowEngine:
    """Drives runs: dispatch ready nodes, persist every transition,
    park on approvals, resume after crashes.

    One dispatcher loop per run per process — :meth:`dispatch` is
    idempotent through an atomic check-and-register with no await in
    between — and up to ``max_parallel`` node attempts in flight.
    Every read-modify-write of a run record goes through
    :meth:`_mutate`, which serialises transitions on one write lock
    and reloads inside it, so concurrent node attempts cannot
    overwrite each other's persisted state. Cross-process double
    dispatch is a v1 non-goal: deployments run one engine process, and
    anything a second process races with is caught by the restart
    recovery sweep.
    """

    _MAX_LOOP_CRASHES = 3
    """How many consecutive dispatcher-loop crashes one run tolerates
    before the heartbeat gives up and fails it."""

    def __init__(
        self,
        storage: StorageBase,
        message_bus: MessageBus,
        chat_runner: ChatRunner,
        judge_agent_factory: JudgeAgentFactory,
        max_parallel: int = 4,
        channel_clients: "ChannelClients | None" = None,
    ) -> None:
        """Initialize the engine.

        Args:
            storage (`StorageBase`): Application storage.
            message_bus (`MessageBus`): Broadcast bus for run events;
                a publish failure is logged, never fatal.
            chat_runner (`ChatRunner`): Drives one agent turn to
                completion (``ChatService.run`` in production).
            judge_agent_factory (`JudgeAgentFactory`): Ensures the
                generic judge agents exist and returns their ids.
            max_parallel (`int`, defaults to ``4``): Node attempts the
                engine runs concurrently across all its runs.
            channel_clients (`ChannelClients | None`, optional): The
                factory notify nodes send through; ``None`` in a
                process without the channel subsystem, whose notify
                nodes then fail as missing channels.
        """
        self._storage = storage
        self._bus = message_bus
        self._handlers = NodeHandlers(
            storage,
            chat_runner,
            judge_agent_factory,
            channel_clients,
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._rescues: set[asyncio.Task] = set()
        self._crash_counts: dict[str, int] = {}
        self._closed = False
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, user_id: str, run_id: str) -> None:
        """Start the dispatcher loop for a run, idempotently.

        The live-task check and the registration below have no await
        between them, so they are atomic under asyncio's cooperative
        scheduling: of two racing dispatch calls — two approvals on
        different parked nodes of one run, a dispatch during recovery —
        exactly one spawns a loop. Everything that used to be probed
        here before spawning (existence, terminal status, the
        PENDING→RUNNING flip) moved into :meth:`_loop`'s head, so this
        body never yields at all.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to drive.
        """
        if self._closed:
            # A closed engine drives nothing; the next process's
            # recovery sweep picks the run up.
            return
        live = self._tasks.get(run_id)
        if live is not None and not live.done():
            return
        # create_task schedules without running the loop, so the task
        # is registered — and visible to any concurrent dispatch —
        # before this coroutine can yield.
        task = asyncio.create_task(self._run_loop(user_id, run_id))
        self._tasks[run_id] = task
        task.add_done_callback(
            lambda done, rid=run_id: self._forget_task(rid, done),
        )

    def _forget_task(self, run_id: str, task: asyncio.Task) -> None:
        """Drop the run's loop task once it ends, logging any crash.

        Crashes normally never reach the task: :meth:`_run_loop`
        catches them to run the heartbeat. This is the backstop for
        anything that escapes even that handler.
        """
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Workflow run %r dispatcher loop crashed.",
                run_id,
                exc_info=task.exception(),
            )
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)

    async def _run_loop(self, user_id: str, run_id: str) -> None:
        """Run :meth:`_loop` for one run, acting as its heartbeat.

        A dispatcher loop that dies of an exception — a storage fault
        between transitions, a bug in the loop itself — would strand a
        non-terminal run with nobody driving it until a restart. The
        wrapper catches the crash, counts it per run, and re-dispatches
        after a beat; a run whose loop keeps crashing is failed out
        through :meth:`_fail_run` once the cap is hit. A clean return
        (park, finalize, nothing to do) resets the counter, and
        cancellation is passed through untouched.
        """
        try:
            await self._loop(user_id, run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            attempt = self._crash_counts.get(run_id, 0) + 1
            self._crash_counts[run_id] = attempt
            try:
                await self._handle_loop_crash(user_id, run_id, attempt, exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                # The heartbeat itself failing must not strand the
                # run silently on top of the original crash.
                logger.error(
                    "Workflow run %r crash handling failed.",
                    run_id,
                    exc_info=True,
                )
        else:
            self._crash_counts.pop(run_id, None)

    async def _handle_loop_crash(
        self,
        user_id: str,
        run_id: str,
        attempt: int,
        exc: BaseException,
    ) -> None:
        """React to one dispatcher-loop crash; see :meth:`_run_loop`.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run whose loop crashed.
            attempt (`int`): Which consecutive crash this is.
            exc (`BaseException`): The crash itself, for the log.
        """
        if attempt >= self._MAX_LOOP_CRASHES:
            logger.warning(
                "Workflow run %r dispatcher loop crashed %d times; "
                "giving up and failing the run.",
                run_id,
                attempt,
            )
            self._crash_counts.pop(run_id, None)
            # Nodes still RUNNING were interrupted mid-attempt; mark
            # them failed so the retry endpoint can reset them.
            await self._fail_run(
                user_id,
                run_id,
                "engine loop crashed repeatedly",
                interrupt_running=True,
            )
            return
        logger.warning(
            "Workflow run %r dispatcher loop crashed (attempt %d of "
            "%d); re-dispatching.",
            run_id,
            attempt,
            self._MAX_LOOP_CRASHES,
            exc_info=exc,
        )
        self._schedule_rescue(user_id, run_id, reason="a loop crash")

    def _schedule_rescue(
        self,
        user_id: str,
        run_id: str,
        *,
        reason: str,
    ) -> None:
        """Spawn (and track) one delayed re-dispatch task.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to re-dispatch.
            reason (`str`): Why, for the re-dispatch's log line.
        """
        rescue = asyncio.create_task(
            self._redispatch_after_crash(user_id, run_id, reason),
        )
        self._rescues.add(rescue)
        rescue.add_done_callback(self._rescues.discard)

    async def _redispatch_after_crash(
        self,
        user_id: str,
        run_id: str,
        reason: str,
    ) -> None:
        """Re-dispatch a run, once the dust settles.

        The beat lets the outgoing task leave the registry and any
        in-flight writes settle; the checks then re-derive whether a
        dispatch is still wanted (a live task took over, the run
        finished or was failed in the meantime) instead of assuming
        it. Attempts the crash interrupted mid-flight are reconciled
        first, so the fresh loop never plans around a stuck-RUNNING
        node. Never raises: the caller is a fire-and-forget task.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to re-dispatch.
            reason (`str`): Why the re-dispatch was scheduled.
        """
        try:
            await asyncio.sleep(0.5)
            if self._closed:
                return
            live = self._tasks.get(run_id)
            if live is not None and not live.done():
                return
            run = await self._storage.get_workflow_run(user_id, run_id)
            if run is None or run.data.status in _RUN_TERMINAL:
                self._crash_counts.pop(run_id, None)
                return
            if any(
                state.status is RunStatus.RUNNING
                for state in run.data.node_states.values()
            ):
                if not await self._settle_interrupted(
                    user_id,
                    run_id,
                    "interrupted by loop crash",
                ):
                    self._crash_counts.pop(run_id, None)
                    return
            logger.info(
                "Re-dispatching workflow run %r after %s.",
                run_id,
                reason,
            )
            await self.dispatch(user_id, run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            logger.error(
                "Workflow run %r re-dispatch failed.",
                run_id,
                exc_info=True,
            )

    async def _settle_interrupted(
        self,
        user_id: str,
        run_id: str,
        cause: str,
    ) -> bool:
        """Settle nodes a dead loop left mid-attempt.

        The same reconciliation restart recovery applies: an
        interrupted attempt with budget left goes back to PENDING
        with *cause* recorded (its try already counted against the
        budget); one whose budget is gone fails the run through
        :meth:`_fail_run` — which also finalizes it, cancelling the
        nodes the failure made unreachable.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to settle.
            cause (`str`): Failure text recorded on the nodes.

        Returns:
            `bool`: False when the run was failed (nothing left to
                drive); True when it is still drivable.
        """
        exhausted: list[str] = []

        def reconcile(record: WorkflowRunRecord):
            events, spent = self._reconcile_interrupted(record, cause)
            exhausted.extend(spent)
            return events

        await self._mutate(user_id, run_id, reconcile)
        if exhausted:
            # interrupt_running also fails any further stuck-RUNNING
            # node — by construction one whose budget is gone too.
            await self._fail_run(
                user_id,
                run_id,
                f"Node {exhausted[0]!r} failed: {cause}",
                node_id=exhausted[0],
                node_error=cause,
                interrupt_running=True,
            )
            return False
        return True

    async def close(self) -> None:
        """Stop the engine: cancel every live loop and rescue task.

        Storage and the message bus must never close underneath a
        running loop, so the lifespan calls this before tearing the
        stack down. Records are deliberately left as they are — a run
        mid-flight stays RUNNING, exactly what a crashed process
        leaves behind and what the next process's
        :meth:`recover_stale_runs` resumes.
        """
        self._closed = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        rescues = [task for task in self._rescues if not task.done()]
        for task in (*tasks, *rescues):
            task.cancel()
        if tasks or rescues:
            await asyncio.gather(*tasks, *rescues, return_exceptions=True)
        self._tasks.clear()
        self._rescues.clear()

    async def _loop(self, user_id: str, run_id: str) -> None:
        """Dispatch ready nodes until the run parks, finishes or fails.

        The entry checks are what ``dispatch`` probes before spawning:
        a missing run logs and stops (routers pre-check existence; a
        vanished run is worth a log line, not a traceback), a terminal
        run has nothing to drive, and a PENDING run is flipped to
        RUNNING before the first pass — so a crash after the flip
        leaves a run the recovery sweep will resume. Each pass then
        reloads the run fresh, so transitions persisted by node
        attempts (including retries) are always seen. A pass that
        finds nothing runnable either parks the run on a WAITING_HUMAN
        node or finalizes it — no PENDING node can be left behind.
        """
        run = await self._storage.get_workflow_run(user_id, run_id)
        if run is None:
            logger.warning("Dispatch skipped: run %r not found.", run_id)
            return
        if run.data.status in _RUN_TERMINAL:
            return
        if run.data.status is RunStatus.PENDING:
            await self._mutate(user_id, run_id, _resume_run)
        while True:
            run = await self._storage.get_workflow_run(user_id, run_id)
            if run is None:
                return
            graph = run.data.graph
            states = run.data.node_states
            if (
                run.data.status in _RUN_TERMINAL
                or run_is_finished(graph, states)
            ):
                await self._finalize(user_id, run)
                return
            ready = ready_nodes(graph, states)
            if not ready:
                if any(
                    state.status is RunStatus.WAITING_HUMAN
                    for state in states.values()
                ):
                    if await self._park(user_id, run_id):
                        return
                    # The park decision re-evaluated under the lock
                    # found the world moved — a verdict made a
                    # successor ready or resolved the parked node.
                    # Re-plan on the next pass instead of stranding
                    # the run parked.
                    continue
                # Nothing can dispatch and nobody is parked, so every
                # PENDING node is unreachable (an un-taken branch).
                # Finalizing cancels them and completes the run along
                # the path it actually took.
                await self._finalize(user_id, run)
                return
            if run.data.status is not RunStatus.RUNNING:
                await self._mutate(user_id, run_id, _resume_run)
            await asyncio.gather(
                *(
                    self._dispatch_node(user_id, run_id, node_id)
                    for node_id in sorted(ready)
                ),
            )

    async def _dispatch_node(
        self,
        user_id: str,
        run_id: str,
        node_id: str,
    ) -> None:
        """One attempt of one node: record it, run the handler, record
        the outcome.

        The RUNNING state with ``attempts + 1`` is persisted *before*
        the handler awaits, so a crash mid-attempt is visible to
        recovery as an interrupted (budget-consuming) try. The handler
        mutates the run it was handed (its session-id stash); each
        outcome transition reloads fresh under the write lock and
        re-adopts that stash, so a reload can never lose it.

        Raises propagate: ``asyncio.CancelledError`` from a run cancel
        tears through the loop untouched, and anything the handler did
        not translate to :class:`NodeFailure` already was.
        """
        async with self._semaphore:
            chosen: WorkflowNode | None = None

            def start(record: WorkflowRunRecord):
                nonlocal chosen
                if record.data.status in _RUN_TERMINAL:
                    return []
                node = next(
                    (
                        candidate
                        for candidate in record.data.graph.nodes
                        if candidate.id == node_id
                    ),
                    None,
                )
                if node is None:
                    return []
                state = record.data.node_states.setdefault(
                    node_id,
                    NodeRunState(),
                )
                if state.status is not RunStatus.PENDING:
                    return []
                chosen = node
                state.status = RunStatus.RUNNING
                state.attempts += 1
                state.error = None
                return [(node_id, RunStatus.RUNNING, None)]

            live = await self._mutate(user_id, run_id, start)
            if live is None or chosen is None:
                return
            node = chosen
            try:
                output = await self._handlers.handle(user_id, live, node)
            except ApprovalPending:

                def park(record: WorkflowRunRecord):
                    self._adopt_sessions(live, record)
                    state = record.data.node_states.setdefault(
                        node_id,
                        NodeRunState(),
                    )
                    state.status = RunStatus.WAITING_HUMAN
                    return [(node_id, RunStatus.WAITING_HUMAN, None)]

                await self._mutate(user_id, run_id, park)
                return
            except NodeFailure as exc:
                message = str(exc)
                exhausted = False

                def fail(record: WorkflowRunRecord):
                    nonlocal exhausted
                    self._adopt_sessions(live, record)
                    state = record.data.node_states.setdefault(
                        node_id,
                        NodeRunState(),
                    )
                    state.error = message
                    if (
                        record.data.status in _RUN_TERMINAL
                        and record.data.status is not RunStatus.FAILED
                    ):
                        # The run finalized while this attempt was in
                        # flight: record the node's own failure, but
                        # never resurrect or re-fail a finished run.
                        # (An already-FAILED run may be updated
                        # idempotently below.)
                        state.status = RunStatus.FAILED
                        logger.warning(
                            "Node %r failed after run %r finalized; "
                            "keeping run status %r.",
                            node_id,
                            record.id,
                            record.data.status.value,
                        )
                        return [(node_id, RunStatus.FAILED, message)]
                    if self._retry_left(node, state.attempts):
                        # Back to PENDING with the failure text kept as
                        # the latest error; the next loop pass runs a
                        # fresh attempt into the same sessions.
                        state.status = RunStatus.PENDING
                        return [(node_id, RunStatus.PENDING, message)]
                    state.status = RunStatus.FAILED
                    exhausted = True
                    return [(node_id, RunStatus.FAILED, message)]

                persisted = await self._mutate(user_id, run_id, fail)
                if exhausted and persisted is not None:
                    # The budget is gone: one shared transition fails
                    # the run and finalizes it (cancelling unreachable
                    # nodes), exactly like every other exhaustion path.
                    await self._fail_run(
                        user_id,
                        run_id,
                        f"Node {node_id!r} failed: {message}",
                        node_id=node_id,
                        node_error=message,
                    )
                return

            def succeed(record: WorkflowRunRecord):
                self._adopt_sessions(live, record)
                record.data.context[node_id] = output
                state = record.data.node_states.setdefault(
                    node_id,
                    NodeRunState(),
                )
                state.status = RunStatus.SUCCEEDED
                state.output = output
                state.error = None
                return [(node_id, RunStatus.SUCCEEDED, None)]

            await self._mutate(user_id, run_id, succeed)

    async def _park(self, user_id: str, run_id: str) -> bool:
        """Park the run as WAITING_HUMAN — unless the world moved.

        The decision is re-evaluated on the freshly reloaded record
        under the write lock: a verdict that landed after the loop's
        pass read (making a successor ready, or resolving the parked
        node) skips the persist entirely, and the caller re-plans
        instead of stranding the run parked.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to park.

        Returns:
            `bool`: True when the run was parked; False when a fresh
            look found it should not be.
        """
        parked = False

        def park_if_still_stalled(record: WorkflowRunRecord):
            nonlocal parked
            if ready_nodes(record.data.graph, record.data.node_states):
                return []
            if not any(
                state.status is RunStatus.WAITING_HUMAN
                for state in record.data.node_states.values()
            ):
                return []
            record.data.status = RunStatus.WAITING_HUMAN
            parked = True
            return [(None, RunStatus.WAITING_HUMAN, None)]

        await self._mutate(user_id, run_id, park_if_still_stalled)
        return parked

    @staticmethod
    def _retry_left(node: WorkflowNode, attempts: int) -> bool:
        """Whether one more attempt of *node* fits its retry budget.

        Agent and notify nodes get ``max_retries`` retries on top of
        the first try (total tries = ``max_retries + 1``); a
        ``retry_until`` loop gets ``max_attempts`` tries in total.
        Everything else — condition, approval, ``iterate`` loops —
        runs exactly once.

        Args:
            node (`WorkflowNode`): The node that just failed.
            attempts (`int`): Attempts already recorded for it.

        Returns:
            `bool`: True when another attempt may run.
        """
        cfg = node.config
        if isinstance(cfg, (AgentNodeConfig, NotifyNodeConfig)):
            return attempts <= cfg.max_retries
        if isinstance(cfg, LoopNodeConfig) and cfg.mode == "retry_until":
            return attempts < cfg.max_attempts
        return False

    def _reconcile_interrupted(
        self,
        record: WorkflowRunRecord,
        cause: str,
    ) -> tuple[
        list[tuple[str | None, RunStatus, str | None]],
        list[str],
    ]:
        """Classify the stuck-RUNNING nodes of *record*, in place.

        One budget decision shared by both interruption contexts —
        restart recovery and the crash rescue: a node whose budget
        still allows a try is flipped back to PENDING with *cause* as
        its error (the interrupted attempt already consumed one), and
        the ids of the rest are returned for the caller to fail the
        run with.

        Args:
            record (`WorkflowRunRecord`): The run record to
                reconcile; mutated in place.
            cause (`str`): Failure text recorded on the nodes.

        Returns:
            `tuple[list, list]`: The PENDING-transition events, and
                the ids whose retry budget is exhausted.
        """
        events: list[tuple[str | None, RunStatus, str | None]] = []
        exhausted: list[str] = []
        for node in record.data.graph.nodes:
            state = record.data.node_states.get(node.id)
            if state is None or state.status is not RunStatus.RUNNING:
                continue
            state.error = cause
            if self._retry_left(node, state.attempts):
                state.status = RunStatus.PENDING
                events.append((node.id, RunStatus.PENDING, cause))
            else:
                exhausted.append(node.id)
        return events, exhausted

    async def _fail_run(
        self,
        user_id: str,
        run_id: str,
        error: str,
        *,
        node_id: str | None = None,
        node_error: str | None = None,
        interrupt_running: bool = False,
    ) -> None:
        """Fail a run (and the node that exhausted its budget), then
        walk it to its terminal record.

        The one "fail the run" transition every exhaustion path
        shares: a node's retry budget gone in :meth:`_dispatch_node`,
        an interrupted attempt's budget gone in recovery, and the
        heartbeat giving up on a crashing loop. Delegates the
        terminal record — unreachable-node cancellation,
        ``finished_at``, the terminal event — to :meth:`_finalize`, so
        a recovery-path failure leaves exactly the record a live-path
        failure leaves.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to fail.
            error (`str`): The run-level failure text.
            node_id (`str | None`, optional): The node whose budget
                ran out, when there is one; it is persisted FAILED.
            node_error (`str | None`, optional): The node-level
                failure text; defaults to *error*.
            interrupt_running (`bool`, optional): Also fail every node
                still RUNNING — attempts a dead loop interrupted
                mid-flight, which the retry endpoint could otherwise
                never reset.
        """
        node_text = node_error if node_error is not None else error

        def fail(record: WorkflowRunRecord):
            events: list[tuple[str | None, RunStatus, str | None]] = []
            if node_id is not None:
                state = record.data.node_states.setdefault(
                    node_id,
                    NodeRunState(),
                )
                state.status = RunStatus.FAILED
                state.error = node_text
                events.append((node_id, RunStatus.FAILED, node_text))
            if interrupt_running:
                for node in record.data.graph.nodes:
                    state = record.data.node_states.setdefault(
                        node.id,
                        NodeRunState(),
                    )
                    if state.status is RunStatus.RUNNING:
                        state.status = RunStatus.FAILED
                        state.error = node_text
                        events.append((node.id, RunStatus.FAILED, node_text))
            if (
                record.data.status in _RUN_TERMINAL
                and record.data.status is not RunStatus.FAILED
            ):
                # Never resurrect or re-fail a run that finished as
                # something else (a cancel, a raced finalize); the
                # node-level records above still land.
                return events
            record.data.status = RunStatus.FAILED
            record.data.error = error
            record.data.finished_at = datetime.now()
            events.append((None, RunStatus.FAILED, error))
            return events

        persisted = await self._mutate(user_id, run_id, fail)
        if persisted is not None:
            await self._finalize(user_id, persisted)

    async def _finalize(self, user_id: str, run: WorkflowRunRecord) -> None:
        """Walk a run to its terminal record.

        Every still-PENDING node is unreachable by construction (the
        loop only finalizes when nothing can dispatch), so each is
        CANCELLED. The run status is then derived from node states —
        any FAILED node fails the run; otherwise the run succeeded
        along the branches it took — unless a terminal status was
        already persisted (a node exhausted its budget, a cancel),
        which is kept as-is. ``finished_at`` is stamped and the
        terminal event published.

        The decision is re-validated under the write lock: callers
        decide from a snapshot, and a fresh record that is drivable
        again (ready nodes), parked (a WAITING_HUMAN node) or still
        working (a RUNNING attempt) is never finalized off the stale
        decision — runnable nodes would be cancelled and the run
        derived terminal around work it never saw settle. Whoever
        moved the record owns driving it.
        """

        def finish(record: WorkflowRunRecord):
            events: list[tuple[str | None, RunStatus, str | None]] = []
            if (
                record.data.status not in _RUN_TERMINAL
                and (
                    ready_nodes(record.data.graph, record.data.node_states)
                    or any(
                        state.status
                        in (RunStatus.RUNNING, RunStatus.WAITING_HUMAN)
                        for state in record.data.node_states.values()
                    )
                )
            ):
                return []
            for node in record.data.graph.nodes:
                state = record.data.node_states.setdefault(
                    node.id,
                    NodeRunState(),
                )
                if state.status is RunStatus.PENDING:
                    state.status = RunStatus.CANCELLED
                    events.append((node.id, RunStatus.CANCELLED, None))
            if record.data.status not in _RUN_TERMINAL:
                failed = [
                    node.id
                    for node in record.data.graph.nodes
                    if record.data.node_states[node.id].status
                    is RunStatus.FAILED
                ]
                if failed:
                    record.data.status = RunStatus.FAILED
                    if not record.data.error:
                        # Defensive: the failure path always records
                        # one; a hand-crafted state may not.
                        record.data.error = f"Node {failed[0]!r} failed."
                else:
                    record.data.status = RunStatus.SUCCEEDED
            record.data.finished_at = datetime.now()
            events.append((None, record.data.status, record.data.error))
            return events

        await self._mutate(run.user_id, run.id, finish)

    # ------------------------------------------------------------------
    # Human decisions and lifecycle
    # ------------------------------------------------------------------

    async def approve(
        self,
        user_id: str,
        run_id: str,
        node_id: str,
        verdict: str,
        note: str | None,
    ) -> WorkflowRunRecord:
        """Record a human verdict on a parked node and resume the run.

        The verdict doubles as the branch label (``output["branch"]``)
        the planner matches against the node's labelled outgoing edges,
        so the labelled successor is exactly what the approver chose.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The parked run.
            node_id (`str`): The WAITING_HUMAN node.
            verdict (`str`): ``approve`` or ``reject``.
            note (`str | None`): Free-text reason, kept on the
                blackboard beside the verdict.

        Returns:
            `WorkflowRunRecord`: The persisted record with the verdict
            applied, just before the resumed dispatch.

        Raises:
            `HTTPException`: 404 when the run is missing; 409 when the
                node is not awaiting approval or the run already
                reached a terminal state (a parallel branch can fail
                the run while this one sits parked); 422 on an unknown
                verdict (defensive — routers constrain it too).
        """
        run = await self._storage.get_workflow_run(user_id, run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found.",
            )
        if run.data.status in _RUN_TERMINAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Run is no longer active.",
            )
        state = run.data.node_states.get(node_id)
        if state is None or state.status is not RunStatus.WAITING_HUMAN:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Node is not awaiting approval.",
            )
        if verdict not in _VERDICTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Verdict must be one of {_VERDICTS}, got {verdict!r}.",
            )
        output = {"branch": verdict, "verdict": verdict, "note": note}

        def record_verdict(item: WorkflowRunRecord):
            if item.data.status in _RUN_TERMINAL:
                # The run finalized between the pre-check and this
                # locked write — never resurrect it with a verdict.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Run is no longer active.",
                )
            node_state = item.data.node_states.setdefault(
                node_id,
                NodeRunState(),
            )
            if node_state.status is not RunStatus.WAITING_HUMAN:
                # Another verdict landed first; nothing to apply.
                return []
            node_state.status = RunStatus.SUCCEEDED
            node_state.output = output
            item.data.context[node_id] = output
            item.data.status = RunStatus.RUNNING
            return [
                (node_id, RunStatus.SUCCEEDED, None),
                (None, RunStatus.RUNNING, None),
            ]

        persisted = await self._mutate(user_id, run_id, record_verdict)
        if persisted is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found.",
            )
        await self.dispatch(user_id, run_id)
        return persisted

    async def cancel(self, user_id: str, run_id: str) -> WorkflowRunRecord:
        """Cancel a run: stop its loop, cancel every non-terminal node.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to cancel.

        Returns:
            `WorkflowRunRecord`: The persisted, cancelled record.

        Raises:
            `HTTPException`: 404 when the run is missing.
        """
        run = await self._storage.get_workflow_run(user_id, run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found.",
            )
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pylint: disable=broad-except
                # A loop that died of something else mid-cancellation
                # must not mask the cancel the caller asked for.
                logger.error(
                    "Workflow run %r loop raised while being cancelled.",
                    run_id,
                    exc_info=True,
                )
        # Reloaded after the loop settled: its final persists count.

        def record_cancel(item: WorkflowRunRecord):
            events: list[tuple[str | None, RunStatus, str | None]] = []
            for node in item.data.graph.nodes:
                node_state = item.data.node_states.setdefault(
                    node.id,
                    NodeRunState(),
                )
                if node_state.status not in _NODE_TERMINAL:
                    node_state.status = RunStatus.CANCELLED
                    events.append((node.id, RunStatus.CANCELLED, None))
            if item.data.status not in _RUN_TERMINAL:
                item.data.status = RunStatus.CANCELLED
                item.data.finished_at = datetime.now()
                events.append((None, RunStatus.CANCELLED, None))
            return events

        persisted = await self._mutate(user_id, run_id, record_cancel)
        if persisted is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found.",
            )
        return persisted

    async def retry(self, user_id: str, run_id: str) -> WorkflowRunRecord:
        """Reset a FAILED run to PENDING and dispatch it again.

        The failed node and the nodes cancelled around it (unreachable
        downstream of the failure) go back to PENDING with a fresh
        attempt budget — a manual retry is the caller's explicit
        choice to start over on those nodes; succeeded nodes keep
        their outputs and sessions.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The failed run.

        Returns:
            `WorkflowRunRecord`: The persisted record, PENDING again,
            just before the fresh dispatch.

        Raises:
            `HTTPException`: 404 when the run is missing; 409 when the
                run did not fail.
        """
        run = await self._storage.get_workflow_run(user_id, run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found.",
            )
        if run.data.status is not RunStatus.FAILED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only failed runs can be retried.",
            )

        def record_retry(item: WorkflowRunRecord):
            if item.data.status is not RunStatus.FAILED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Only failed runs can be retried.",
                )
            events: list[tuple[str | None, RunStatus, str | None]] = []
            for node in item.data.graph.nodes:
                node_state = item.data.node_states.setdefault(
                    node.id,
                    NodeRunState(),
                )
                if node_state.status in (
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                ):
                    node_state.status = RunStatus.PENDING
                    node_state.attempts = 0
                    node_state.output = None
                    node_state.error = None
                    events.append((node.id, RunStatus.PENDING, None))
            item.data.status = RunStatus.PENDING
            item.data.error = None
            item.data.finished_at = None
            events.append((None, RunStatus.PENDING, None))
            return events

        persisted = await self._mutate(user_id, run_id, record_retry)
        if persisted is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id!r} not found.",
            )
        live = self._tasks.get(run_id)
        if live is not None and not live.done():
            # The loop that failed the run can still be tearing down
            # — its final pass read the run as FAILED and its finalize
            # declines the reset record — and the dispatch below
            # would no-op against the dying task. One delayed
            # re-dispatch covers that window; the direct dispatch
            # covers the task already being gone.
            self._schedule_rescue(user_id, run_id, reason="a retry")
        await self.dispatch(user_id, run_id)
        return persisted

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover_stale_runs(self) -> int:
        """Reconcile runs a previous process left non-terminal.

        A node stuck RUNNING was an attempt a restart interrupted: it
        goes back to PENDING when its budget allows another try, else
        FAILED (failing the run). A WAITING_HUMAN approval older than
        its ``timeout_hours`` applies its ``timeout_branch`` —
        ``fail`` ends the run as FAILED, any other label behaves as if
        that branch had been chosen. Runs that can continue are
        dispatched afresh; still-parked ones are left parked. Each
        record is reconciled in isolation: one unreadable or failing
        run is logged and skipped, never aborting the sweep (or the
        app boot that runs it).

        Returns:
            `int`: How many runs recovery rewrote or resumed.
        """
        touched = 0
        for record in await self._storage.list_active_workflow_runs():
            try:
                touched += await self._recover_one(record)
            except Exception:  # pylint: disable=broad-except
                logger.error(
                    "Recovering workflow run %r failed; skipping it.",
                    record.id,
                    exc_info=True,
                )
        return touched

    async def _recover_one(self, record: WorkflowRunRecord) -> int:
        """Reconcile one active run; see :meth:`recover_stale_runs`.

        Args:
            record (`WorkflowRunRecord`): The active run to reconcile.

        Returns:
            `int`: 1 when recovery rewrote or resumed it, else 0.
        """
        user_id = record.user_id
        run_id = record.id
        now = datetime.now()
        events, exhausted = self._reconcile_interrupted(
            record,
            "interrupted by restart",
        )
        for node in record.data.graph.nodes:
            node_state = record.data.node_states.get(node.id)
            if node_state is None:
                continue
            cfg = node.config
            if (
                node_state.status is RunStatus.WAITING_HUMAN
                and isinstance(cfg, HumanApprovalNodeConfig)
                and now - record.updated_at
                > timedelta(hours=cfg.timeout_hours)
            ):
                if cfg.timeout_branch == "fail":
                    node_state.status = RunStatus.FAILED
                    node_state.error = "approval timed out"
                    record.data.status = RunStatus.FAILED
                    record.data.error = (
                        f"Node {node.id!r} failed: approval timed out."
                    )
                    record.data.finished_at = now
                else:
                    node_state.status = RunStatus.SUCCEEDED
                    node_state.output = {"branch": cfg.timeout_branch}
                    record.data.context[node.id] = node_state.output
                events.append(
                    (node.id, node_state.status, node_state.error),
                )
        acted = bool(events)
        if events:
            await self._storage.upsert_workflow_run(user_id, record)
            for node_id, run_status, error in events:
                await self._publish(
                    run_id,
                    node_id=node_id,
                    status=run_status,
                    error=error,
                )
        for node_id in exhausted:
            await self._fail_run(
                user_id,
                run_id,
                f"Node {node_id!r} failed: interrupted by restart",
                node_id=node_id,
                node_error="interrupted by restart",
            )
        if exhausted:
            # The run is FAILED and finalized; nothing left to resume.
            return 1
        graph = record.data.graph
        states = record.data.node_states
        if (
            record.data.status in _RUN_TERMINAL
            or run_is_finished(graph, states)
        ):
            await self._finalize(user_id, record)
            return 1
        ready = ready_nodes(graph, states)
        waiting = any(
            node_state.status is RunStatus.WAITING_HUMAN
            for node_state in states.values()
        )
        if waiting and not ready:
            if record.data.status is not RunStatus.WAITING_HUMAN:
                # Crash between parking the node and parking the run;
                # finish parking it.
                record.data.status = RunStatus.WAITING_HUMAN
                await self._storage.upsert_workflow_run(user_id, record)
                await self._publish(
                    run_id,
                    node_id=None,
                    status=RunStatus.WAITING_HUMAN,
                )
                acted = True
            return 1 if acted else 0
        await self.dispatch(user_id, run_id)
        return 1

    # ------------------------------------------------------------------
    # Transition plumbing
    # ------------------------------------------------------------------

    async def _mutate(
        self,
        user_id: str,
        run_id: str,
        apply: Callable[
            [WorkflowRunRecord],
            list[tuple[str | None, RunStatus, str | None]],
        ],
    ) -> WorkflowRunRecord | None:
        """One persisted run transition, serialised on the write lock.

        The run is reloaded inside the lock so a transition never
        overwrites a sibling attempt's concurrent write; *apply*
        mutates the fresh record in place and returns the events to
        publish (``node_id`` None addresses the run itself); the
        persist happens before the publishes, so subscribers see the
        state that is already durable.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to transition.
            apply (`Callable`): The mutation, run under the lock.

        Returns:
            `WorkflowRunRecord | None`: The persisted record, or None
            when the run no longer exists.
        """
        async with self._write_lock:
            record = await self._storage.get_workflow_run(user_id, run_id)
            if record is None:
                return None
            events = apply(record)
            record = await self._storage.upsert_workflow_run(user_id, record)
            for node_id, run_status, error in events:
                await self._publish(
                    run_id,
                    node_id=node_id,
                    status=run_status,
                    error=error,
                )
            return record

    @staticmethod
    def _adopt_sessions(
        live: WorkflowRunRecord,
        record: WorkflowRunRecord,
    ) -> None:
        """Copy the session-id stash *live* accrued during its attempt
        onto the freshly reloaded *record*.

        Handlers stash session ids into the run they were handed in
        memory only; reloading for a transition is the one place that
        in-memory state could be dropped.
        """
        stash = live.data.context.get(_SESSIONS_KEY)
        if isinstance(stash, dict) and stash:
            sessions = record.data.context.setdefault(_SESSIONS_KEY, {})
            if isinstance(sessions, dict):
                sessions.update(stash)

    async def _publish(
        self,
        run_id: str,
        *,
        node_id: str | None,
        status: RunStatus,
        error: str | None = None,
    ) -> None:
        """Broadcast one transition on the run's channel.

        Payload schema: ``{"type": "node" | "run", "run_id", "node_id",
        "status", "error"}`` — ``type``/``node_id`` None means the run
        itself. A bus failure is logged and swallowed: the run record
        in storage is the source of truth, the bus is a live nudge.

        Args:
            run_id (`str`): The run that transitioned.
            node_id (`str | None`): The node that transitioned, or
                None for a run-level transition.
            status (`RunStatus`): The state entered.
            error (`str | None`, optional): Failure text, if any.
        """
        payload = {
            "type": "run" if node_id is None else "node",
            "run_id": run_id,
            "node_id": node_id,
            "status": status.value,
            "error": error,
        }
        try:
            await self._bus.publish(f"workflow_run:{run_id}", payload)
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Workflow run %r event publish failed.",
                run_id,
                exc_info=True,
            )


def _resume_run(record: WorkflowRunRecord):
    """Transition helper: mark the run itself RUNNING."""
    record.data.status = RunStatus.RUNNING
    return [(None, RunStatus.RUNNING, None)]


async def make_judge_agent_factory(
    storage: StorageBase,
) -> JudgeAgentFactory:
    """Build a judge-agent factory bound to *storage*.

    The factory ensures a generic judge agent exists once per user per
    name and then serves its id from cache. The system prompt is
    workflow-agnostic — the label list / verdict criteria always ride
    the user message — so an agent minted by an earlier process is
    still correct. Lookup-first therefore: an existing record with the
    requested name is reused (a restart no longer mints a fresh row
    per boot), and only a missing one is upserted.

    Args:
        storage (`StorageBase`): Where judge agents are looked up and
            upserted.

    Returns:
        `JudgeAgentFactory`: The cached factory.
    """
    cache: dict[tuple[str, str], str] = {}

    async def factory(user_id: str, name: str, system_prompt: str) -> str:
        key = (user_id, name)
        if key not in cache:
            for agent in await storage.list_agents(user_id):
                if agent.data.name == name:
                    # Info, not silent: a deliberate name collision
                    # (a user agent named like a judge marker) adopts
                    # that agent, and this makes it diagnosable.
                    logger.info(
                        "Reusing existing %r agent %r as judge",
                        name,
                        agent.id,
                    )
                    cache[key] = agent.id
                    break
            else:
                cache[key] = await storage.upsert_agent(
                    user_id,
                    AgentRecord(
                        user_id=user_id,
                        data=AgentData(
                            name=name,
                            system_prompt=system_prompt,
                        ),
                    ),
                )
        return cache[key]

    return factory
