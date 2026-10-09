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

from agentscope.app._service._workflow_engine import WorkflowEngine
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
        tasks = list(self.engine._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
        done = await self._poll(run.id, {RunStatus.SUCCEEDED})
        listener.cancel()

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
