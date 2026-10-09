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
"""
import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from fastapi import HTTPException, status

from ..message_bus import MessageBus
from ..storage import (
    AgentData,
    AgentNodeConfig,
    AgentRecord,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    NodeRunState,
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
    idempotent while a loop is alive — and up to ``max_parallel`` node
    attempts in flight. Every read-modify-write of a run record goes
    through :meth:`_mutate`, which serialises transitions on one write
    lock and reloads inside it, so concurrent node attempts cannot
    overwrite each other's persisted state. Cross-process double
    dispatch is a v1 non-goal: deployments run one engine process, and
    anything a second process races with is caught by the restart
    recovery sweep.
    """

    def __init__(
        self,
        storage: StorageBase,
        message_bus: MessageBus,
        chat_runner: ChatRunner,
        judge_agent_factory: JudgeAgentFactory,
        max_parallel: int = 4,
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
        """
        self._storage = storage
        self._bus = message_bus
        self._handlers = NodeHandlers(storage, chat_runner, judge_agent_factory)
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, user_id: str, run_id: str) -> None:
        """Start the dispatcher loop for a run, idempotently.

        While a loop task for the run is alive this is a no-op. A
        PENDING run is flipped to RUNNING before the loop spawns, so a
        crash between here and the first node persist still leaves a
        run the recovery sweep will resume.

        Args:
            user_id (`str`): The run's owner.
            run_id (`str`): The run to drive.
        """
        live = self._tasks.get(run_id)
        if live is not None and not live.done():
            return
        run = await self._storage.get_workflow_run(user_id, run_id)
        if run is None:
            # Routers pre-check existence; a vanished run is worth a
            # log line, not a traceback.
            logger.warning("Dispatch skipped: run %r not found.", run_id)
            return
        if run.data.status in _RUN_TERMINAL:
            return
        if run.data.status is RunStatus.PENDING:
            await self._mutate(user_id, run_id, _resume_run)
        task = asyncio.create_task(self._loop(user_id, run_id))
        self._tasks[run_id] = task
        task.add_done_callback(
            lambda done, rid=run_id: self._forget_task(rid, done),
        )

    def _forget_task(self, run_id: str, task: asyncio.Task) -> None:
        """Drop the run's loop task once it ends, logging any crash."""
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Workflow run %r dispatcher loop crashed.",
                run_id,
                exc_info=task.exception(),
            )
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)

    async def _loop(self, user_id: str, run_id: str) -> None:
        """Dispatch ready nodes until the run parks, finishes or fails.

        Each pass reloads the run fresh, so transitions persisted by
        node attempts (including retries) are always seen. A pass that
        finds nothing runnable either parks the run on a WAITING_HUMAN
        node or finalizes it — no PENDING node can be left behind.
        """
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
                    await self._mutate(user_id, run_id, _park_run)
                    return
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

                def fail(record: WorkflowRunRecord):
                    self._adopt_sessions(live, record)
                    state = record.data.node_states.setdefault(
                        node_id,
                        NodeRunState(),
                    )
                    state.error = message
                    if self._retry_left(node, state.attempts):
                        # Back to PENDING with the failure text kept as
                        # the latest error; the next loop pass runs a
                        # fresh attempt into the same sessions.
                        state.status = RunStatus.PENDING
                        return [(node_id, RunStatus.PENDING, message)]
                    state.status = RunStatus.FAILED
                    record.data.status = RunStatus.FAILED
                    record.data.error = (
                        f"Node {node_id!r} failed: {message}"
                    )
                    record.data.finished_at = datetime.now()
                    return [
                        (node_id, RunStatus.FAILED, message),
                        (None, RunStatus.FAILED, record.data.error),
                    ]

                await self._mutate(user_id, run_id, fail)
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

    @staticmethod
    def _retry_left(node: WorkflowNode, attempts: int) -> bool:
        """Whether one more attempt of *node* fits its retry budget.

        Agent nodes get ``max_retries`` retries on top of the first
        try (total tries = ``max_retries + 1``); a ``retry_until``
        loop gets ``max_attempts`` tries in total. Everything else —
        condition, approval, ``iterate`` loops — runs exactly once.

        Args:
            node (`WorkflowNode`): The node that just failed.
            attempts (`int`): Attempts already recorded for it.

        Returns:
            `bool`: True when another attempt may run.
        """
        cfg = node.config
        if isinstance(cfg, AgentNodeConfig):
            return attempts <= cfg.max_retries
        if isinstance(cfg, LoopNodeConfig) and cfg.mode == "retry_until":
            return attempts < cfg.max_attempts
        return False

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
        """

        def finish(record: WorkflowRunRecord):
            events: list[tuple[str | None, RunStatus, str | None]] = []
            for node in record.data.graph.nodes:
                state = record.data.node_states.setdefault(
                    node.id,
                    NodeRunState(),
                )
                if state.status is RunStatus.PENDING:
                    state.status = RunStatus.CANCELLED
                    events.append((node.id, RunStatus.CANCELLED, None))
            if record.data.status not in _RUN_TERMINAL:
                statuses = [
                    record.data.node_states[node.id].status
                    for node in record.data.graph.nodes
                ]
                if any(item is RunStatus.FAILED for item in statuses):
                    record.data.status = RunStatus.FAILED
                    if not record.data.error:
                        record.data.error = "workflow stalled: no runnable nodes."
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
                node is not awaiting approval; 422 on an unknown
                verdict (defensive — routers constrain it too).
        """
        run = await self._storage.get_workflow_run(user_id, run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow run {run_id!r} not found.",
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
                detail=f"Workflow run {run_id!r} not found.",
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
                detail=f"Workflow run {run_id!r} not found.",
            )
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
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
                detail=f"Workflow run {run_id!r} not found.",
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
                detail=f"Workflow run {run_id!r} not found.",
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
                detail=f"Workflow run {run_id!r} not found.",
            )
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
        dispatched afresh; still-parked ones are left parked.

        Returns:
            `int`: How many runs recovery rewrote or resumed.
        """
        touched = 0
        for record in await self._storage.list_active_workflow_runs():
            user_id = record.user_id
            run_id = record.id
            now = datetime.now()
            events: list[tuple[str | None, RunStatus, str | None]] = []
            for node in record.data.graph.nodes:
                node_state = record.data.node_states.get(node.id)
                if node_state is None:
                    continue
                if node_state.status is RunStatus.RUNNING:
                    node_state.error = "interrupted by restart"
                    if self._retry_left(node, node_state.attempts):
                        node_state.status = RunStatus.PENDING
                    else:
                        node_state.status = RunStatus.FAILED
                        record.data.status = RunStatus.FAILED
                        record.data.error = (
                            f"Node {node.id!r} failed: "
                            "interrupted by restart"
                        )
                        record.data.finished_at = now
                    events.append(
                        (node.id, node_state.status, node_state.error),
                    )
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
            graph = record.data.graph
            states = record.data.node_states
            if (
                record.data.status in _RUN_TERMINAL
                or run_is_finished(graph, states)
            ):
                await self._finalize(user_id, record)
                acted = True
                touched += 1
                continue
            ready = ready_nodes(graph, states)
            waiting = any(
                node_state.status is RunStatus.WAITING_HUMAN
                for node_state in states.values()
            )
            if waiting and not ready:
                if record.data.status is not RunStatus.WAITING_HUMAN:
                    # Crash between parking the node and parking the
                    # run; finish parking it.
                    record.data.status = RunStatus.WAITING_HUMAN
                    await self._storage.upsert_workflow_run(user_id, record)
                    await self._publish(
                        run_id,
                        node_id=None,
                        status=RunStatus.WAITING_HUMAN,
                    )
                    acted = True
                if acted:
                    touched += 1
                continue
            await self.dispatch(user_id, run_id)
            touched += 1
        return touched

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


def _park_run(record: WorkflowRunRecord):
    """Transition helper: park the run itself."""
    record.data.status = RunStatus.WAITING_HUMAN
    return [(None, RunStatus.WAITING_HUMAN, None)]


def _resume_run(record: WorkflowRunRecord):
    """Transition helper: mark the run itself RUNNING."""
    record.data.status = RunStatus.RUNNING
    return [(None, RunStatus.RUNNING, None)]


async def make_judge_agent_factory(
    storage: StorageBase,
) -> JudgeAgentFactory:
    """Build a judge-agent factory bound to *storage*.

    The factory ensures a generic judge agent exists once per user per
    name and then serves its id from cache. The system prompt holds
    only the label list — the specific instruction always rides the
    user message — so caching by name is safe. A restarted process
    upserts a fresh record instead of looking the old one up: judge
    agents carry no model of their own (the handler passes a
    session-level one) and judge sessions are run-scoped, so the only
    cost is one extra agent row per process start.

    Args:
        storage (`StorageBase`): Where judge agents are upserted.

    Returns:
        `JudgeAgentFactory`: The cached factory.
    """
    cache: dict[tuple[str, str], str] = {}

    async def factory(user_id: str, name: str, system_prompt: str) -> str:
        key = (user_id, name)
        if key not in cache:
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
