# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The workflow run engine: dispatch, retries, park/resume, recovery.

Storage is seeded directly and the engine is constructed by hand —
the engine is the only thing under test, not the app around it.
"""
import asyncio
import time
from datetime import datetime, timedelta
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi import HTTPException

from agentscope.app._service._workflow_engine import (
    WorkflowEngine,
    make_judge_agent_factory,
)
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    AgentChatConfig,
    AgentData,
    AgentNodeConfig,
    AgentRecord,
    ChatModelConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    NodeRunState,
    RedisStorage,
    RunStatus,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRunData,
    WorkflowRunRecord,
)
from agentscope.message import AssistantMsg, Msg

USER = "user-1"


def make_storage() -> RedisStorage:
    """A RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def _model(model: str = "m") -> ChatModelConfig:
    """A chat model config, distinct per model name."""
    return ChatModelConfig(
        type="openai_credential",
        credential_id="c-1",
        model=model,
        parameters={},
    )


async def _seed_agent(
    storage: RedisStorage,
    name: str,
    model: ChatModelConfig | None = None,
) -> str:
    """Create an agent template and return its id."""
    return await storage.upsert_agent(
        USER,
        AgentRecord(
            user_id=USER,
            data=AgentData(
                name=name,
                chat_config=AgentChatConfig(chat_model_config=model),
            ),
        ),
    )


def _agent_node(
    node_id: str,
    agent_id: str,
    prompt_template: str = "{{input.topic}}",
    **kwargs: Any,
) -> WorkflowNode:
    """An agent node referencing *agent_id*."""
    return WorkflowNode(
        id=node_id,
        config=AgentNodeConfig(
            agent_id=agent_id,
            prompt_template=prompt_template,
            **kwargs,
        ),
    )


def _edge(source: str, target: str, branch: str | None = None) -> WorkflowEdge:
    """A uniquely identified edge."""
    suffix = f":{branch}" if branch is not None else ""
    return WorkflowEdge(
        id=f"{source}->{target}{suffix}",
        source=source,
        target=target,
        branch=branch,
    )


def _run(
    graph: WorkflowGraph,
    *,
    run_status: RunStatus = RunStatus.PENDING,
    node_states: dict[str, NodeRunState] | None = None,
    context: dict[str, Any] | None = None,
) -> WorkflowRunRecord:
    """A run record shaped like the ones the engine drives."""
    return WorkflowRunRecord(
        user_id=USER,
        data=WorkflowRunData(
            workflow_id="wf-1",
            workflow_version=1,
            workflow_name="pipeline",
            graph=graph,
            input={"topic": "Q3 revenue"},
            status=run_status,
            chat_model_config=_model(),
            context=dict(context or {}),
            node_states={
                node_id: state.model_copy(deep=True)
                for node_id, state in (node_states or {}).items()
            },
        ),
    )


class _ScriptedRunner:
    """Canned replies per agent, mirroring a real chat turn.

    The incoming user message is persisted before the reply — the
    page shape prod ``ChatService.run`` leaves behind. Each agent has
    its own queue consumed in order (the last entry repeats, so a
    single canned reply serves any number of turns); agents without a
    queue share ``default``. A ``BaseException`` entry is raised
    instead of filed, standing in for a runner that blows up.
    """

    def __init__(self, storage: RedisStorage) -> None:
        self._storage = storage
        self.replies: dict[str, list[str | BaseException]] = {}
        self.default: list[str | BaseException] = []
        self.calls: list[tuple[str, str, str, Msg]] = []

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        self.calls.append((user_id, session_id, agent_id, msg))
        await self._storage.upsert_message(user_id, session_id, msg)
        queue = self.replies.get(agent_id) or self.default
        if queue:
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            reply = "canned"
        if isinstance(reply, BaseException):
            raise reply
        await self._storage.upsert_message(
            user_id,
            session_id,
            AssistantMsg(name=agent_id, content=reply),
        )


class _FlippingRunner(_ScriptedRunner):
    """A turn that fails, but only after flipping its run terminal.

    Reproduces what a raced finalize leaves behind: the node attempt
    is still mid-flight when the run record says the run already
    ended.
    """

    def __init__(self, storage: RedisStorage, run_id: str) -> None:
        super().__init__(storage)
        self._run_id = run_id

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        run = await self._storage.get_workflow_run(user_id, self._run_id)
        if run is not None:
            run.data.status = RunStatus.SUCCEEDED
            run.data.finished_at = datetime.now()
            await self._storage.upsert_workflow_run(user_id, run)
        raise RuntimeError("late failure")


class _HangingRunner(_ScriptedRunner):
    """A turn that never completes until released.

    Parks a node attempt mid-flight inside a live engine loop, the
    state ``close()`` must tear down without corrupting.
    """

    def __init__(self, storage: RedisStorage) -> None:
        super().__init__(storage)
        self.release = asyncio.Event()

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        await self.release.wait()


class _FakeJudgeFactory:
    """Returns a pre-created judge agent id."""

    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id

    async def __call__(
        self,
        user_id: str,
        name: str,
        system_prompt: str,
    ) -> str:
        return self._agent_id


class WorkflowEngineTest(IsolatedAsyncioTestCase):
    """Dispatch, retries, park/resume and restart recovery."""

    async def asyncSetUp(self) -> None:
        self.storage = make_storage()
        self.bus = InMemoryMessageBus()
        await self.bus.__aenter__()
        self.runner = _ScriptedRunner(self.storage)
        self.judge_id = await _seed_agent(self.storage, "judge", _model("jm"))
        self.engine = WorkflowEngine(
            storage=self.storage,
            message_bus=self.bus,
            chat_runner=self.runner,
            judge_agent_factory=_FakeJudgeFactory(self.judge_id),
        )

    async def asyncTearDown(self) -> None:
        await self.engine.close()
        await self.bus.aclose()

    # -- helpers ------------------------------------------------------

    async def _seed_run(
        self,
        record: WorkflowRunRecord,
    ) -> WorkflowRunRecord:
        """Persist a run record and return the stored copy."""
        return await self.storage.upsert_workflow_run(USER, record)

    async def _poll(
        self,
        run_id: str,
        statuses: set[RunStatus],
        timeout: float = 2.0,
    ) -> WorkflowRunRecord:
        """Fetch the run until it reaches one of *statuses*."""
        deadline = time.monotonic() + timeout
        run: WorkflowRunRecord | None = None
        while time.monotonic() < deadline:
            run = await self.storage.get_workflow_run(USER, run_id)
            if run is not None and run.data.status in statuses:
                return run
            await asyncio.sleep(0.01)
        states = (
            {k: v.status.value for k, v in run.data.node_states.items()}
            if run is not None
            else None
        )
        self.fail(
            f"Run never reached {sorted(s.value for s in statuses)}; "
            f"last status={run.data.status if run else None}, "
            f"nodes={states}.",
        )

    async def _poll_event(
        self,
        events: list[dict],
        expected: tuple[str, str | None, str],
        timeout: float = 2.0,
    ) -> None:
        """Wait until *expected* (type, node_id, status) was drained by
        the listener — the deterministic way to know a transition was
        both persisted and published."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if expected in {
                (event["type"], event["node_id"], event["status"])
                for event in events
            }:
                return
            await asyncio.sleep(0.01)
        seen = [
            (event["type"], event["node_id"], event["status"])
            for event in events
        ]
        self.fail(f"Event {expected} never arrived; saw {seen}.")

    async def _poll_node(
        self,
        run_id: str,
        node_id: str,
        target: RunStatus,
        timeout: float = 2.0,
    ) -> WorkflowRunRecord:
        """Fetch the run until *node_id* reaches *target*."""
        deadline = time.monotonic() + timeout
        run: WorkflowRunRecord | None = None
        while time.monotonic() < deadline:
            run = await self.storage.get_workflow_run(USER, run_id)
            if run is not None:
                state = run.data.node_states.get(node_id, NodeRunState())
                if state.status is target:
                    return run
            await asyncio.sleep(0.01)
        states = (
            {k: v.status.value for k, v in run.data.node_states.items()}
            if run is not None
            else None
        )
        self.fail(
            f"Node {node_id!r} never reached {target.value}; "
            f"run status={run.data.status if run else None}, "
            f"nodes={states}.",
        )

    def _state(self, run: WorkflowRunRecord, node_id: str) -> NodeRunState:
        """One node's state, failing loudly when absent."""
        state = run.data.node_states.get(node_id)
        self.assertIsNotNone(state, f"node {node_id!r} has no state")
        assert state is not None  # for the type checker
        return state

    def _status(self, run: WorkflowRunRecord, node_id: str) -> RunStatus:
        """One node's status; absent means PENDING, as the planner
        reads it."""
        state = run.data.node_states.get(node_id, NodeRunState())
        return state.status

    async def _listen(self, run_id: str) -> tuple[list[dict], asyncio.Task]:
        """Subscribe to the run's event channel before dispatching."""
        events: list[dict] = []
        ready = asyncio.Event()

        async def consume() -> None:
            async for payload in self.bus.subscribe(
                f"workflow_run:{run_id}",
                on_ready=ready.set,
            ):
                events.append(payload)

        task = asyncio.create_task(consume())
        await ready.wait()
        return events, task

    async def _age(self, run: WorkflowRunRecord, hours: float) -> None:
        """Rewrite the stored run with an older ``updated_at``.

        ``upsert_workflow_run`` stamps ``updated_at`` on every write,
        so the timeout path can only be exercised by ageing the
        persisted record behind storage's back.
        """
        aged = run.model_copy(deep=True)
        aged.updated_at = datetime.now() - timedelta(hours=hours)
        key = self.storage._key(
            self.storage.key_config.workflow_run,
            user_id=USER,
            run_id=run.id,
        )
        await self.storage._client.set(key, aged.model_dump_json())

    async def _parked_approval_run(
        self,
        *,
        timeout_branch: str = "fail",
        targets: tuple[str, ...] = ("done",),
    ) -> WorkflowRunRecord:
        """A run parked on approval: agent -> signoff -> target(s)."""
        writer_id = await _seed_agent(self.storage, "writer", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", writer_id),
                WorkflowNode(
                    id="signoff",
                    config=HumanApprovalNodeConfig(
                        timeout_hours=24.0,
                        timeout_branch=timeout_branch,
                    ),
                ),
                *(_agent_node(t, writer_id) for t in targets),
            ],
            edges=[
                _edge("a", "signoff"),
                *(_edge("signoff", t, "approve") for t in targets),
            ],
        )
        run = await self._seed_run(
            _run(
                graph,
                run_status=RunStatus.WAITING_HUMAN,
                node_states={
                    "a": NodeRunState(
                        status=RunStatus.SUCCEEDED,
                        attempts=1,
                        output={"text": "canned", "structured": None},
                    ),
                    "signoff": NodeRunState(
                        status=RunStatus.WAITING_HUMAN,
                        attempts=1,
                    ),
                },
                context={"a": {"text": "canned", "structured": None}},
            ),
        )
        return run

    # -- 1. linear dispatch ------------------------------------------

    async def test_linear_two_agents_succeed(self) -> None:
        """Two sequential agent nodes both run, in order, and the run
        ends SUCCEEDED with the active index emptied."""
        writer_id = await _seed_agent(self.storage, "writer", _model())
        polisher_id = await _seed_agent(self.storage, "polisher", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", writer_id, prompt_template="Draft {{input.topic}}"),
                _agent_node("b", polisher_id, prompt_template="Polish {{nodes.a.output.text}}"),
            ],
            edges=[_edge("a", "b")],
        )
        run = await self._seed_run(_run(graph))
        events, listener = await self._listen(run.id)

        await self.engine.dispatch(USER, run.id)
        # Wait on the published terminal event, not the stored status:
        # the persist lands a tick before the subscriber drains it.
        await self._poll_event(events, ("run", None, "SUCCEEDED"))
        listener.cancel()
        done = await self.storage.get_workflow_run(USER, run.id)
        self.assertIsNotNone(done)
        assert done is not None  # for the type checker

        self.assertEqual(done.data.context["a"]["text"], "canned")
        self.assertEqual(done.data.context["b"]["text"], "canned")
        self.assertEqual(
            self.runner.calls[1][3].get_text_content(),
            "Polish canned",
        )
        for node_id in ("a", "b"):
            state = self._state(done, node_id)
            self.assertEqual(state.status, RunStatus.SUCCEEDED)
            self.assertEqual(state.attempts, 1)
            self.assertIsNone(state.error)
        self.assertIsNotNone(done.data.finished_at)
        self.assertEqual(await self.storage.list_active_workflow_runs(), [])

        seen = {
            (event["type"], event["node_id"], event["status"])
            for event in events
        }
        self.assertIn(("run", None, "RUNNING"), seen)
        self.assertIn(("node", "a", "RUNNING"), seen)
        self.assertIn(("node", "a", "SUCCEEDED"), seen)
        self.assertIn(("node", "b", "RUNNING"), seen)
        self.assertIn(("node", "b", "SUCCEEDED"), seen)
        self.assertIn(("run", None, "SUCCEEDED"), seen)

    # -- 2. condition and unreachable nodes --------------------------

    async def test_condition_run_cancels_untaken_branch(self) -> None:
        """A rule-picked branch runs; the un-taken branch's subtree is
        cancelled as unreachable and the run still SUCCEEDs."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                WorkflowNode(
                    id="route",
                    config=ConditionNodeConfig(
                        branches=[
                            ConditionBranch(
                                label="yes",
                                rule="input.ok == true",
                            ),
                            ConditionBranch(
                                label="no",
                                rule="input.ok == false",
                            ),
                        ],
                    ),
                ),
                _agent_node("go", worker_id),
                _agent_node("stop", worker_id),
            ],
            edges=[
                _edge("route", "go", "yes"),
                _edge("route", "stop", "no"),
            ],
        )
        record = _run(graph)
        record.data.input = {"topic": "t", "ok": True}
        run = await self._seed_run(record)

        await self.engine.dispatch(USER, run.id)
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})

        self.assertEqual(
            self._state(done, "route").output,
            {"branch": "yes"},
        )
        self.assertEqual(self._state(done, "go").status, RunStatus.SUCCEEDED)
        self.assertEqual(self._state(done, "stop").status, RunStatus.CANCELLED)
        self.assertEqual(done.data.context["route"], {"branch": "yes"})
        self.assertEqual(
            await self.storage.list_active_workflow_runs(),
            [],
        )

    # -- 3./4. agent retry budgets -----------------------------------

    async def test_agent_retry_within_budget(self) -> None:
        """Two failures then a success with max_retries=2: three
        attempts in one session, node SUCCEEDED."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        self.runner.replies[worker_id] = [
            RuntimeError("transient one"),
            RuntimeError("transient two"),
            "recovered",
        ]
        graph = WorkflowGraph(
            nodes=[_agent_node("flaky", worker_id, max_retries=2)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))

        await self.engine.dispatch(USER, run.id)
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})

        state = self._state(done, "flaky")
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.attempts, 3)
        self.assertIsNone(state.error)
        self.assertEqual(done.data.context["flaky"]["text"], "recovered")
        # Retries rejoin one conversation.
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(
            len({call[1] for call in self.runner.calls}),
            1,
        )

    async def test_agent_retry_exhausted_fails_run(self) -> None:
        """max_retries=0 and a persistent failure: the node and the
        run both FAIL, the error naming the node."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        self.runner.replies[worker_id] = [RuntimeError("kaput")]
        graph = WorkflowGraph(
            nodes=[_agent_node("die", worker_id, max_retries=0)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))

        await self.engine.dispatch(USER, run.id)
        done = await self._poll(run.id, {RunStatus.FAILED})

        state = self._state(done, "die")
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertEqual(state.attempts, 1)
        self.assertIn("kaput", state.error)
        self.assertIn("'die'", done.data.error)
        self.assertIn("kaput", done.data.error)
        self.assertIsNotNone(done.data.finished_at)
        self.assertEqual(await self.storage.list_active_workflow_runs(), [])

    # -- 5.-8. approval park and verdicts ----------------------------

    async def test_approval_parks_run(self) -> None:
        """An approval node parks the run as WAITING_HUMAN."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", worker_id),
                WorkflowNode(id="signoff", config=HumanApprovalNodeConfig()),
                _agent_node("done", worker_id),
            ],
            edges=[_edge("a", "signoff"), _edge("signoff", "done", "approve")],
        )
        run = await self._seed_run(_run(graph))

        await self.engine.dispatch(USER, run.id)
        parked = await self._poll(run.id, {RunStatus.WAITING_HUMAN})

        self.assertEqual(parked.data.status, RunStatus.WAITING_HUMAN)
        self.assertEqual(
            self._state(parked, "signoff").status,
            RunStatus.WAITING_HUMAN,
        )
        self.assertEqual(self._state(parked, "a").status, RunStatus.SUCCEEDED)
        self.assertEqual(self._status(parked, "done"), RunStatus.PENDING)
        self.assertIsNone(parked.data.finished_at)
        active = await self.storage.list_active_workflow_runs()
        self.assertEqual([record.id for record in active], [run.id])

    async def test_approve_resumes_successor(self) -> None:
        """An approve verdict lands on the blackboard and the labelled
        successor runs to completion."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", worker_id),
                WorkflowNode(id="signoff", config=HumanApprovalNodeConfig()),
                _agent_node(
                    "done",
                    worker_id,
                    prompt_template="Ship: {{nodes.signoff.output.note}}",
                ),
            ],
            edges=[_edge("a", "signoff"), _edge("signoff", "done", "approve")],
        )
        run = await self._seed_run(_run(graph))
        await self.engine.dispatch(USER, run.id)
        await self._poll(run.id, {RunStatus.WAITING_HUMAN})

        verdict = await self.engine.approve(
            USER,
            run.id,
            "signoff",
            "approve",
            "ship it",
        )
        self.assertEqual(verdict.data.context["signoff"]["verdict"], "approve")
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})

        self.assertEqual(
            self._state(done, "signoff").output,
            {"branch": "approve", "verdict": "approve", "note": "ship it"},
        )
        self.assertEqual(self._state(done, "done").status, RunStatus.SUCCEEDED)
        self.assertEqual(
            self.runner.calls[-1][3].get_text_content(),
            "Ship: ship it",
        )

    async def test_reject_takes_reject_branch(self) -> None:
        """A reject verdict runs the reject-edge successor and cancels
        the approve-edge one as unreachable."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", worker_id),
                WorkflowNode(id="signoff", config=HumanApprovalNodeConfig()),
                _agent_node("ship", worker_id),
                _agent_node("abort", worker_id),
            ],
            edges=[
                _edge("a", "signoff"),
                _edge("signoff", "ship", "approve"),
                _edge("signoff", "abort", "reject"),
            ],
        )
        run = await self._seed_run(_run(graph))
        await self.engine.dispatch(USER, run.id)
        await self._poll(run.id, {RunStatus.WAITING_HUMAN})

        await self.engine.approve(USER, run.id, "signoff", "reject", "nope")
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})

        self.assertEqual(done.data.context["signoff"]["verdict"], "reject")
        self.assertEqual(self._state(done, "ship").status, RunStatus.CANCELLED)
        self.assertEqual(
            self._state(done, "abort").status,
            RunStatus.SUCCEEDED,
        )

    async def test_verdict_edge_cases(self) -> None:
        """404 on a missing run, 409 on a non-parked node, 422 on an
        unknown verdict."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", worker_id),
                WorkflowNode(id="signoff", config=HumanApprovalNodeConfig()),
                _agent_node("done", worker_id),
            ],
            edges=[_edge("a", "signoff"), _edge("signoff", "done", "approve")],
        )
        run = await self._seed_run(_run(graph))
        await self.engine.dispatch(USER, run.id)
        await self._poll(run.id, {RunStatus.WAITING_HUMAN})

        with self.assertRaises(HTTPException) as caught:
            await self.engine.approve(USER, "run-missing", "signoff", "approve", None)
        self.assertEqual(caught.exception.status_code, 404)
        # The engine's noun matches WorkflowService.get_run's.
        self.assertEqual(
            caught.exception.detail,
            "Run 'run-missing' not found.",
        )

        with self.assertRaises(HTTPException) as caught:
            await self.engine.approve(USER, run.id, "a", "approve", None)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail,
            "Node is not awaiting approval.",
        )

        with self.assertRaises(HTTPException) as caught:
            await self.engine.approve(USER, run.id, "signoff", "maybe", None)
        self.assertEqual(caught.exception.status_code, 422)

        # The run is still parked after all that.
        parked = await self.storage.get_workflow_run(USER, run.id)
        assert parked is not None  # for the type checker
        self.assertEqual(parked.data.status, RunStatus.WAITING_HUMAN)

    # -- 9./10. cancel and retry --------------------------------------

    async def test_cancel_parked_run(self) -> None:
        """Cancelling a parked run cancels its non-terminal nodes and
        empties the active index."""
        run = await self._parked_approval_run()
        cancelled = await self.engine.cancel(USER, run.id)

        self.assertEqual(cancelled.data.status, RunStatus.CANCELLED)
        self.assertEqual(self._state(cancelled, "a").status, RunStatus.SUCCEEDED)
        self.assertEqual(
            self._state(cancelled, "signoff").status,
            RunStatus.CANCELLED,
        )
        self.assertEqual(
            self._state(cancelled, "done").status,
            RunStatus.CANCELLED,
        )
        self.assertIsNotNone(cancelled.data.finished_at)
        self.assertEqual(await self.storage.list_active_workflow_runs(), [])

        # A cancelled run is not retryable.
        with self.assertRaises(HTTPException) as caught:
            await self.engine.retry(USER, run.id)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_retry_failed_run_resets_attempts(self) -> None:
        """retry() gives the failed node a fresh budget; the re-run
        succeeds with attempts back at one."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        self.runner.replies[worker_id] = [RuntimeError("kaput")]
        graph = WorkflowGraph(
            nodes=[_agent_node("die", worker_id, max_retries=0)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))
        await self.engine.dispatch(USER, run.id)
        failed = await self._poll(run.id, {RunStatus.FAILED})
        self.assertEqual(self._state(failed, "die").attempts, 1)

        self.runner.replies[worker_id] = ["all good"]
        retried = await self.engine.retry(USER, run.id)
        self.assertEqual(retried.data.status, RunStatus.PENDING)
        self.assertIsNone(retried.data.error)

        done = await self._poll(run.id, {RunStatus.SUCCEEDED})
        state = self._state(done, "die")
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.attempts, 1)
        self.assertEqual(done.data.context["die"]["text"], "all good")

    # -- 11.-14. restart recovery -------------------------------------

    async def test_recover_retries_interrupted_node(self) -> None:
        """A node stuck RUNNING from a crash goes back to PENDING when
        the budget allows, and the run then completes."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("work", worker_id)],
            edges=[],
        )
        run = await self._seed_run(
            _run(
                graph,
                run_status=RunStatus.RUNNING,
                node_states={
                    "work": NodeRunState(
                        status=RunStatus.RUNNING,
                        attempts=1,
                    ),
                },
            ),
        )

        touched = await self.engine.recover_stale_runs()

        self.assertEqual(touched, 1)
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})
        state = self._state(done, "work")
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        # The interrupted attempt plus the retry.
        self.assertEqual(state.attempts, 2)

    async def test_recover_approval_timeout_fail_branch(self) -> None:
        """A parked approval past its timeout with timeout_branch
        'fail' fails the node and the run, cancelling the successor."""
        run = await self._parked_approval_run(timeout_branch="fail")
        await self._age(run, hours=48)

        touched = await self.engine.recover_stale_runs()

        self.assertEqual(touched, 1)
        failed = await self.storage.get_workflow_run(USER, run.id)
        assert failed is not None  # for the type checker
        self.assertEqual(failed.data.status, RunStatus.FAILED)
        self.assertIn("approval timed out", failed.data.error)
        self.assertEqual(
            self._state(failed, "signoff").status,
            RunStatus.FAILED,
        )
        self.assertEqual(self._state(failed, "done").status, RunStatus.CANCELLED)
        self.assertIsNotNone(failed.data.finished_at)
        self.assertEqual(await self.storage.list_active_workflow_runs(), [])

    async def test_recover_approval_timeout_escalate_branch(self) -> None:
        """A non-'fail' timeout branch behaves like that branch was
        chosen: the escalate edge runs, the other edges cancel."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                WorkflowNode(
                    id="signoff",
                    config=HumanApprovalNodeConfig(
                        timeout_hours=24.0,
                        timeout_branch="escalate",
                    ),
                ),
                _agent_node("ship", worker_id),
                _agent_node("abort", worker_id),
                _agent_node("page", worker_id),
            ],
            edges=[
                _edge("signoff", "ship", "approve"),
                _edge("signoff", "abort", "reject"),
                _edge("signoff", "page", "escalate"),
            ],
        )
        run = await self._seed_run(
            _run(
                graph,
                run_status=RunStatus.WAITING_HUMAN,
                node_states={
                    "signoff": NodeRunState(
                        status=RunStatus.WAITING_HUMAN,
                        attempts=1,
                    ),
                },
            ),
        )
        await self._age(run, hours=48)

        touched = await self.engine.recover_stale_runs()

        self.assertEqual(touched, 1)
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})
        self.assertEqual(
            self._state(done, "signoff").output,
            {"branch": "escalate"},
        )
        self.assertEqual(self._state(done, "ship").status, RunStatus.CANCELLED)
        self.assertEqual(self._state(done, "abort").status, RunStatus.CANCELLED)
        self.assertEqual(self._state(done, "page").status, RunStatus.SUCCEEDED)

    async def test_recover_leaves_fresh_park_alone(self) -> None:
        """A parked approval inside its timeout window is untouched."""
        run = await self._parked_approval_run()

        touched = await self.engine.recover_stale_runs()

        self.assertEqual(touched, 0)
        parked = await self.storage.get_workflow_run(USER, run.id)
        assert parked is not None  # for the type checker
        self.assertEqual(parked.data.status, RunStatus.WAITING_HUMAN)
        self.assertEqual(
            self._state(parked, "signoff").status,
            RunStatus.WAITING_HUMAN,
        )
        self.assertEqual(self._status(parked, "done"), RunStatus.PENDING)

    # -- 15./16. dispatch atomicity and terminal guards ---------------

    async def test_concurrent_dispatch_spawns_one_loop(self) -> None:
        """Two racing dispatch() calls spawn exactly one loop task.

        The check for a live task and the registration of the new one
        must be atomic: the pre-fix code checked, then awaited twice
        (load, PENDING->RUNNING persist) before registering, so two
        concurrent callers — two approves on different parked nodes of
        one run — both passed the check and each spawned a loop. The
        second loop then saw the first's in-flight RUNNING node, found
        nothing runnable, and finalized the run mid-flight.

        Fakeredis awaits complete without yielding to the event loop,
        so the storage read is wrapped in an explicit ``sleep(0)`` to
        open the race window deterministically — the interleaving two
        real concurrent approvals produce.
        """
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("a", worker_id)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))

        entries = 0

        async def slow_loop(user_id: str, run_id: str) -> None:
            nonlocal entries
            entries += 1
            await asyncio.sleep(0.2)

        real_get = self.storage.get_workflow_run

        async def yielding_get(user_id: str, run_id: str):
            await asyncio.sleep(0)
            return await real_get(user_id, run_id)

        self.engine._loop = slow_loop  # pylint: disable=attribute-defined-outside-init
        self.storage.get_workflow_run = yielding_get  # pylint: disable=attribute-defined-outside-init
        try:
            await asyncio.gather(
                self.engine.dispatch(USER, run.id),
                self.engine.dispatch(USER, run.id),
            )
        finally:
            self.storage.get_workflow_run = real_get  # pylint: disable=attribute-defined-outside-init

        self.assertEqual(entries, 1)
        live = [t for t in self.engine._tasks.values() if not t.done()]
        self.assertEqual(len(live), 1)

    async def test_late_node_failure_keeps_finalized_run(self) -> None:
        """A node failing after the run finalized records its own
        failure but never overwrites the run's terminal status.

        The node's chat turn flips the run to SUCCEEDED before raising,
        so the failure transition lands on an already-finalized run —
        the artifact a raced finalize (or any terminal write between
        the attempt's start and its outcome) leaves behind.
        """
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("a", worker_id, max_retries=0)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))
        engine = WorkflowEngine(
            storage=self.storage,
            message_bus=self.bus,
            chat_runner=_FlippingRunner(self.storage, run.id),
            judge_agent_factory=_FakeJudgeFactory(self.judge_id),
        )

        with self.assertLogs(
            "agentscope.app._service._workflow_engine",
            level="WARNING",
        ) as logs:
            await engine.dispatch(USER, run.id)
            final = await self._poll_node(run.id, "a", RunStatus.FAILED)
        # Let the loop's final pass settle before asserting.
        await asyncio.sleep(0.05)

        self.assertEqual(final.data.status, RunStatus.SUCCEEDED)
        state = self._state(final, "a")
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertIn("late failure", state.error)
        self.assertTrue(
            any("finalized" in line for line in logs.output),
            logs.output,
        )

    # -- 17.-20. terminal absorption, locked park, sweep isolation ---

    async def test_approve_on_terminal_run_is_409(self) -> None:
        """Approving a leftover parked node on a terminal run answers
        409 and never resurrects the run.

        The shape is real: a parallel branch exhausted its budget
        while this branch sat parked, so the run FAILED with the
        approval node still WAITING_HUMAN.
        """
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", worker_id),
                WorkflowNode(id="signoff", config=HumanApprovalNodeConfig()),
                _agent_node("done", worker_id),
            ],
            edges=[_edge("a", "signoff"), _edge("signoff", "done", "approve")],
        )
        record = _run(
            graph,
            run_status=RunStatus.FAILED,
            node_states={
                "a": NodeRunState(
                    status=RunStatus.SUCCEEDED,
                    attempts=1,
                    output={"text": "canned", "structured": None},
                ),
                "signoff": NodeRunState(
                    status=RunStatus.WAITING_HUMAN,
                    attempts=1,
                ),
            },
            context={"a": {"text": "canned", "structured": None}},
        )
        record.data.error = "Node 'other' failed: budget exhausted."
        run = await self._seed_run(record)

        with self.assertRaises(HTTPException) as caught:
            await self.engine.approve(USER, run.id, "signoff", "approve", None)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "Run is no longer active.")
        final = await self.storage.get_workflow_run(USER, run.id)
        assert final is not None  # for the type checker
        self.assertEqual(final.data.status, RunStatus.FAILED)
        self.assertEqual(
            self._state(final, "signoff").status,
            RunStatus.WAITING_HUMAN,
        )
        self.assertEqual(self._status(final, "done"), RunStatus.PENDING)
        # Nothing dispatched: no successor turn, no live loop.
        self.assertEqual(self.runner.calls, [])
        self.assertNotIn(run.id, self.engine._tasks)

    async def test_verdict_racing_the_park_still_dispatches(self) -> None:
        """A verdict landing between the loop's read and its park
        decision must not strand the run parked.

        The park decision is re-evaluated on the fresh record under
        the write lock; the pre-fix park stamped WAITING_HUMAN over a
        run whose successor had just become ready, and with the loop
        still alive nobody re-dispatched it until the next boot.
        """
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", worker_id),
                WorkflowNode(id="signoff", config=HumanApprovalNodeConfig()),
                _agent_node("done", worker_id),
            ],
            edges=[_edge("a", "signoff"), _edge("signoff", "done", "approve")],
        )
        run = await self._seed_run(_run(graph))

        real_get = self.storage.get_workflow_run
        fired = False

        async def racing_get(user_id: str, run_id: str):
            nonlocal fired
            current = await real_get(user_id, run_id)
            signoff = (
                current.data.node_states.get("signoff")
                if current is not None
                else None
            )
            if (
                not fired
                and current is not None
                and current.data.status is RunStatus.RUNNING
                and signoff is not None
                and signoff.status is RunStatus.WAITING_HUMAN
            ):
                # The verdict lands right after the loop's read: apply
                # it to storage, then hand the loop the stale
                # pre-verdict snapshot it actually observed.
                fired = True
                stale = current.model_copy(deep=True)
                output = {
                    "branch": "approve",
                    "verdict": "approve",
                    "note": "raced",
                }
                signoff.status = RunStatus.SUCCEEDED
                signoff.output = output
                current.data.context["signoff"] = output
                await self.storage.upsert_workflow_run(user_id, current)
                return stale
            return current

        self.storage.get_workflow_run = racing_get  # pylint: disable=attribute-defined-outside-init
        try:
            await self.engine.dispatch(USER, run.id)
            final = await self._poll(run.id, {RunStatus.SUCCEEDED})
        finally:
            self.storage.get_workflow_run = real_get  # pylint: disable=attribute-defined-outside-init

        self.assertEqual(
            self._state(final, "signoff").output,
            {"branch": "approve", "verdict": "approve", "note": "raced"},
        )
        self.assertEqual(self._state(final, "done").status, RunStatus.SUCCEEDED)

    async def test_recovery_skips_poisoned_rows(self) -> None:
        """One corrupt stored run must not abort the recovery sweep."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("work", worker_id)],
            edges=[],
        )
        healthy = await self._seed_run(
            _run(
                graph,
                run_status=RunStatus.RUNNING,
                node_states={
                    "work": NodeRunState(status=RunStatus.RUNNING, attempts=1),
                },
            ),
        )
        poison = await self._seed_run(
            _run(
                graph,
                run_status=RunStatus.RUNNING,
                node_states={
                    "work": NodeRunState(status=RunStatus.RUNNING, attempts=1),
                },
            ),
        )
        key = self.storage._key(
            self.storage.key_config.workflow_run,
            user_id=USER,
            run_id=poison.id,
        )
        await self.storage._client.set(key, "{not valid json")

        with self.assertLogs(level="WARNING") as logs:
            touched = await self.engine.recover_stale_runs()

        self.assertGreaterEqual(touched, 1)
        self.assertTrue(
            any(poison.id in line for line in logs.output),
            logs.output,
        )
        final = await self._poll(healthy.id, {RunStatus.SUCCEEDED})
        self.assertEqual(self._state(final, "work").status, RunStatus.SUCCEEDED)

    async def test_cancel_survives_a_loop_that_raises(self) -> None:
        """Cancelling a loop that dies of a non-cancel error still
        returns the cancelled record instead of propagating to the
        API caller."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        run = await self._seed_run(
            _run(WorkflowGraph(nodes=[_agent_node("a", worker_id)], edges=[])),
        )

        async def stubborn_loop(user_id: str, run_id: str) -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise RuntimeError("cleanup exploded") from None

        self.engine._loop = stubborn_loop  # pylint: disable=attribute-defined-outside-init
        await self.engine.dispatch(USER, run.id)
        await asyncio.sleep(0.01)

        cancelled = await self.engine.cancel(USER, run.id)

        self.assertEqual(cancelled.data.status, RunStatus.CANCELLED)
        self.assertEqual(self._status(cancelled, "a"), RunStatus.CANCELLED)

    # -- 21./22. loop-crash heartbeat ---------------------------------

    async def test_loop_crash_redispatches_and_completes(self) -> None:
        """A dispatcher loop that crashes once is re-dispatched after a
        beat and the run still completes, the crash logged as a
        WARNING instead of stranding the run until restart."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("a", worker_id)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))

        real_loop = self.engine._loop
        entries = 0

        async def crashing_loop(user_id: str, run_id: str) -> None:
            nonlocal entries
            entries += 1
            if entries == 1:
                raise RuntimeError("loop blew up")
            return await real_loop(user_id, run_id)

        self.engine._loop = crashing_loop  # pylint: disable=attribute-defined-outside-init
        try:
            with self.assertLogs(
                "agentscope.app._service._workflow_engine",
                level="WARNING",
            ) as logs:
                await self.engine.dispatch(USER, run.id)
                done = await self._poll(
                    run.id,
                    {RunStatus.SUCCEEDED},
                    timeout=5.0,
                )
        finally:
            self.engine._loop = real_loop  # pylint: disable=attribute-defined-outside-init

        self.assertEqual(self._state(done, "a").status, RunStatus.SUCCEEDED)
        self.assertEqual(done.data.context["a"]["text"], "canned")
        # The crashed loop ran, then a fresh one finished the work.
        self.assertGreaterEqual(entries, 2)
        self.assertTrue(
            any("crashed" in line for line in logs.output),
            logs.output,
        )

    async def test_persistent_loop_crash_fails_run_then_retry_revives(
        self,
    ) -> None:
        """A loop that keeps crashing gives up at the cap: the run is
        FAILED with the crash error, and retry() revives it once the
        loop is healthy again."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("a", worker_id)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))
        real_loop = self.engine._loop
        entries = 0

        async def crashing_loop(user_id: str, run_id: str) -> None:
            nonlocal entries
            entries += 1
            raise RuntimeError("loop blew up")

        self.engine._loop = crashing_loop  # pylint: disable=attribute-defined-outside-init
        try:
            with self.assertLogs(
                "agentscope.app._service._workflow_engine",
                level="WARNING",
            ) as logs:
                await self.engine.dispatch(USER, run.id)
                failed = await self._poll(
                    run.id,
                    {RunStatus.FAILED},
                    timeout=5.0,
                )
            # No further dispatches once the run has given up.
            await asyncio.sleep(0.1)
            self.assertEqual(entries, 3)
        finally:
            self.engine._loop = real_loop  # pylint: disable=attribute-defined-outside-init

        self.assertEqual(failed.data.error, "engine loop crashed repeatedly")
        self.assertIsNotNone(failed.data.finished_at)
        self.assertEqual(await self.storage.list_active_workflow_runs(), [])
        self.assertTrue(
            any("giving up" in line for line in logs.output),
            logs.output,
        )

        # The failed run is recoverable through the retry endpoint.
        retried = await self.engine.retry(USER, run.id)
        self.assertEqual(retried.data.status, RunStatus.PENDING)
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})
        state = self._state(done, "a")
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        self.assertEqual(state.attempts, 1)

    # -- 23. shutdown ---------------------------------------------------

    async def test_close_cancels_midflight_loop_for_recovery(self) -> None:
        """close() cancels live loops without touching storage: the run
        stays RUNNING exactly as a crashed process would leave it, and
        the next process's recovery sweep resumes it with the
        interrupted attempt counted against the budget."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("work", worker_id)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))
        engine = WorkflowEngine(
            storage=self.storage,
            message_bus=self.bus,
            chat_runner=_HangingRunner(self.storage),
            judge_agent_factory=_FakeJudgeFactory(self.judge_id),
        )

        await engine.dispatch(USER, run.id)
        mid = await self._poll_node(run.id, "work", RunStatus.RUNNING)
        self.assertEqual(mid.data.status, RunStatus.RUNNING)
        task = engine._tasks[run.id]

        await engine.close()

        self.assertTrue(task.cancelled())
        self.assertEqual(engine._tasks, {})
        stranded = await self.storage.get_workflow_run(USER, run.id)
        assert stranded is not None  # for the type checker
        self.assertEqual(stranded.data.status, RunStatus.RUNNING)
        self.assertEqual(
            self._state(stranded, "work").status,
            RunStatus.RUNNING,
        )

        # A restarted process sweeps the stranded run and finishes it.
        successor = WorkflowEngine(
            storage=self.storage,
            message_bus=self.bus,
            chat_runner=self.runner,
            judge_agent_factory=_FakeJudgeFactory(self.judge_id),
        )
        touched = await successor.recover_stale_runs()
        self.assertEqual(touched, 1)
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})
        await successor.close()
        state = self._state(done, "work")
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        # The interrupted attempt plus the recovery retry.
        self.assertEqual(state.attempts, 2)

    async def test_recover_exhausted_interruption_cancels_unreachable(
        self,
    ) -> None:
        """An interrupted attempt with no retry budget left fails its
        node and the run through the same path a live failure takes:
        the unreachable downstream node is CANCELLED, not left
        PENDING."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[
                _agent_node("a", worker_id, max_retries=0),
                _agent_node("b", worker_id),
            ],
            edges=[_edge("a", "b")],
        )
        run = await self._seed_run(
            _run(
                graph,
                run_status=RunStatus.RUNNING,
                node_states={
                    "a": NodeRunState(
                        status=RunStatus.RUNNING,
                        attempts=1,
                    ),
                },
            ),
        )

        touched = await self.engine.recover_stale_runs()

        self.assertEqual(touched, 1)
        failed = await self.storage.get_workflow_run(USER, run.id)
        assert failed is not None  # for the type checker
        self.assertEqual(failed.data.status, RunStatus.FAILED)
        self.assertIn("interrupted by restart", failed.data.error)
        self.assertEqual(self._state(failed, "a").status, RunStatus.FAILED)
        self.assertEqual(self._state(failed, "b").status, RunStatus.CANCELLED)
        self.assertIsNotNone(failed.data.finished_at)
        self.assertEqual(await self.storage.list_active_workflow_runs(), [])

    # -- 24.-26. crash reconciliation and finalize hardening ---------

    async def test_crash_with_interrupted_attempt_reconciles(self) -> None:
        """A loop crash between a node's start and outcome writes
        leaves the node stuck RUNNING; the rescue reconciles it back
        to PENDING (budget-aware) and the re-dispatched run completes
        — instead of the run being derived SUCCEEDED around the stuck
        node."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("a", worker_id)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))
        real_mutate = self.engine._mutate
        calls = 0

        async def faulting_mutate(
            user_id: str,
            run_id: str,
            apply: Any,
        ) -> Any:
            # Fault the third transition — the single node's outcome
            # write — after RUNNING (attempt already counted) was
            # persisted by the second.
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("storage fault between transitions")
            return await real_mutate(user_id, run_id, apply)

        self.engine._mutate = faulting_mutate  # pylint: disable=attribute-defined-outside-init
        try:
            with self.assertLogs(
                "agentscope.app._service._workflow_engine",
                level="WARNING",
            ) as logs:
                await self.engine.dispatch(USER, run.id)
                done = await self._poll(
                    run.id,
                    {RunStatus.SUCCEEDED},
                    timeout=5.0,
                )
        finally:
            self.engine._mutate = real_mutate  # pylint: disable=attribute-defined-outside-init

        state = self._state(done, "a")
        self.assertEqual(state.status, RunStatus.SUCCEEDED)
        # The interrupted attempt plus the reconciled retry.
        self.assertEqual(state.attempts, 2)
        self.assertIsNone(state.error)
        self.assertEqual(done.data.context["a"]["text"], "canned")
        self.assertTrue(
            any("crashed" in line for line in logs.output),
            logs.output,
        )

    async def test_crash_with_exhausted_attempt_fails_run(self) -> None:
        """The same crash with no retry budget left fails the node
        and the run through the shared fail path — never a SUCCEEDED
        run around a dead attempt."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("a", worker_id, max_retries=0)],
            edges=[],
        )
        run = await self._seed_run(_run(graph))
        real_mutate = self.engine._mutate
        calls = 0

        async def faulting_mutate(
            user_id: str,
            run_id: str,
            apply: Any,
        ) -> Any:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("storage fault between transitions")
            return await real_mutate(user_id, run_id, apply)

        self.engine._mutate = faulting_mutate  # pylint: disable=attribute-defined-outside-init
        try:
            await self.engine.dispatch(USER, run.id)
            failed = await self._poll(
                run.id,
                {RunStatus.FAILED},
                timeout=5.0,
            )
        finally:
            self.engine._mutate = real_mutate  # pylint: disable=attribute-defined-outside-init

        self.assertEqual(failed.data.status, RunStatus.FAILED)
        self.assertIn("interrupted by loop crash", failed.data.error)
        state = self._state(failed, "a")
        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertIn("interrupted by loop crash", state.error)
        self.assertIsNotNone(failed.data.finished_at)
        self.assertEqual(await self.storage.list_active_workflow_runs(), [])

    async def test_finalize_declines_inflight_record(self) -> None:
        """_finalize never derives a terminal status off a record with
        in-flight (RUNNING) work: the driver that owns the attempt
        settles it, not the finalizer."""
        worker_id = await _seed_agent(self.storage, "worker", _model())
        graph = WorkflowGraph(
            nodes=[_agent_node("a", worker_id)],
            edges=[],
        )
        run = await self._seed_run(
            _run(
                graph,
                run_status=RunStatus.RUNNING,
                node_states={
                    "a": NodeRunState(
                        status=RunStatus.RUNNING,
                        attempts=1,
                    ),
                },
            ),
        )

        await self.engine._finalize(USER, run)

        after = await self.storage.get_workflow_run(USER, run.id)
        assert after is not None  # for the type checker
        self.assertEqual(after.data.status, RunStatus.RUNNING)
        self.assertEqual(self._state(after, "a").status, RunStatus.RUNNING)
        self.assertIsNone(after.data.finished_at)


class JudgeAgentFactoryTest(IsolatedAsyncioTestCase):
    """``make_judge_agent_factory`` reuses judge agents across boots."""

    async def test_lookup_first_within_and_across_boots(self) -> None:
        """Same (user, name) maps to one agent id, even for a factory
        built after a restart: the persisted row is found instead of
        a fresh one being minted per process start."""
        storage = make_storage()
        boot_one = await make_judge_agent_factory(storage)

        branch_one = await boot_one(
            USER,
            "workflow-branch-judge",
            "prompt one",
        )
        # A second call in the same boot (fresh or stale prompt — the
        # agent is shared) must not mint another row either.
        branch_two = await boot_one(
            USER,
            "workflow-branch-judge",
            "prompt two",
        )
        self.assertEqual(branch_one, branch_two)

        # A different name is a different agent.
        verifier = await boot_one(USER, "workflow-verifier", "prompt")
        self.assertNotEqual(verifier, branch_one)

        # "Second boot": a fresh factory (empty cache) over the same
        # storage finds the persisted record.
        boot_two = await make_judge_agent_factory(storage)
        branch_three = await boot_two(
            USER,
            "workflow-branch-judge",
            "prompt three",
        )
        self.assertEqual(branch_three, branch_one)

        names = [agent.data.name for agent in await storage.list_agents(USER)]
        self.assertEqual(names.count("workflow-branch-judge"), 1)
        self.assertEqual(names.count("workflow-verifier"), 1)
