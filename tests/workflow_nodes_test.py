# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""One attempt of each workflow node type, over fakeredis."""
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis

from agentscope.app._service._workflow_nodes import (
    ApprovalPending,
    ChatRunner,
    NodeFailure,
    NodeHandlers,
)
from agentscope.app.storage import (
    AgentChatConfig,
    AgentData,
    AgentNodeConfig,
    AgentRecord,
    ChatModelConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    RedisStorage,
    WorkflowGraph,
    WorkflowNode,
    WorkflowOrigin,
    WorkflowRunData,
    WorkflowRunRecord,
)
from agentscope.message import AssistantMsg, Msg, TextBlock
from agentscope.types import ErrorInfo, ErrorType, ReplyFinishedReason

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
    model: ChatModelConfig | None,
) -> str:
    """Create an agent template and return its id."""
    record = AgentRecord(
        user_id=USER,
        data=AgentData(
            name=name,
            chat_config=AgentChatConfig(chat_model_config=model),
        ),
    )
    return await storage.upsert_agent(USER, record)


def _run(
    nodes: list[WorkflowNode],
    *,
    inputs: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    model: ChatModelConfig | None = None,
) -> WorkflowRunRecord:
    """A running workflow record over the given nodes."""
    return WorkflowRunRecord(
        user_id=USER,
        data=WorkflowRunData(
            workflow_id="wf-1",
            workflow_version=1,
            workflow_name="pipeline",
            graph=WorkflowGraph(nodes=nodes),
            input=inputs or {"topic": "Q3 revenue"},
            chat_model_config=model,
            context=dict(context or {}),
        ),
    )


class _FakeRunner:
    """Records turns and files canned assistant replies.

    The incoming user message is persisted before the reply — the
    page shape prod ``ChatService.run`` leaves behind — and replies
    are consumed in order, the last one repeating, so a single canned
    reply serves any number of turns.
    """

    def __init__(
        self,
        storage: RedisStorage,
        *replies: str,
        structured: dict[str, Any] | None = None,
    ) -> None:
        self._storage = storage
        self._replies = list(replies) or ["canned"]
        self._structured = structured
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
        reply = self._replies.pop(0) if len(self._replies) > 1 else (
            self._replies[0]
        )
        await self._storage.upsert_message(
            user_id,
            session_id,
            AssistantMsg(
                name=agent_id,
                content=reply,
                structured_output=self._structured,
            ),
        )


class _SilentRunner:
    """Records turns but files no reply at all."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, Msg]] = []

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        self.calls.append((user_id, session_id, agent_id, msg))


class _FailedTurnRunner:
    """Files an error-bearing reply — prod's shape for a failed turn.

    ``ChatService.run`` swallows its exceptions and closes the reply
    with an error or a non-COMPLETED reason; the handler must refuse
    to read those as node output.
    """

    def __init__(
        self,
        storage: RedisStorage,
        finished_reason: ReplyFinishedReason,
        error: ErrorInfo | None = None,
    ) -> None:
        self._storage = storage
        self._reason = finished_reason
        self._error = error

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        await self._storage.upsert_message(
            user_id,
            session_id,
            Msg(
                name=agent_id,
                content=[TextBlock(text="partial text")],
                role="assistant",
                finished_reason=self._reason,
                error=self._error,
            ),
        )


class _RaisingRunner:
    """Blows up where the chat runner would."""

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        raise RuntimeError("storage exploded")


class _FakeJudgeFactory:
    """Returns a pre-created judge agent id, recording the prompts."""

    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(
        self,
        user_id: str,
        name: str,
        system_prompt: str,
    ) -> str:
        self.calls.append((user_id, name, system_prompt))
        return self._agent_id


class NodeHandlersTest(IsolatedAsyncioTestCase):
    """Agent, condition, loop and approval attempts."""

    async def asyncSetUp(self) -> None:
        self.storage = make_storage()
        self.judge_id = await _seed_agent(
            self.storage,
            "judge",
            _model("judge-m"),
        )
        self.judge = _FakeJudgeFactory(self.judge_id)

    def _handlers(self, runner: ChatRunner) -> NodeHandlers:
        """The handler set under test."""
        return NodeHandlers(self.storage, runner, self.judge)

    async def _condition(
        self,
        rule: str,
        context: dict[str, Any],
    ) -> str:
        """Run a two-branch condition; returns the taken label.

        The second arm is an llm_judge fallback whose canned reply is
        ``miss``, so a non-matching first rule lands somewhere
        observable instead of failing for a missing judge model.
        """
        node = WorkflowNode(
            id="gate",
            config=ConditionNodeConfig(
                branches=[
                    ConditionBranch(label="hit", rule=rule),
                    ConditionBranch(label="miss", rule="llm_judge: pick"),
                ],
            ),
        )
        output = await self._handlers(
            _FakeRunner(self.storage, "miss"),
        ).handle(USER, _run([node], model=_model(), context=context), node)
        return str(output["branch"])

    async def test_agent_happy_path(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", _model())
        runner = _FakeRunner(self.storage)
        node = WorkflowNode(
            id="research",
            config=AgentNodeConfig(
                agent_id=agent_id,
                prompt_template="Report on {{input.topic}}",
            ),
        )
        run = _run([node])

        output = await self._handlers(runner).handle(USER, run, node)

        self.assertEqual(output, {"text": "canned", "structured": None})
        self.assertEqual(len(runner.calls), 1)
        user_id, session_id, called_agent, msg = runner.calls[0]
        self.assertEqual(user_id, USER)
        self.assertEqual(called_agent, agent_id)
        self.assertEqual(msg.get_text_content(), "Report on Q3 revenue")
        session = await self.storage.get_session(
            USER,
            agent_id,
            session_id,
        )
        self.assertIsNotNone(session)
        assert session is not None  # for the type checker
        self.assertEqual(
            session.origin,
            WorkflowOrigin(workflow_run_id=run.id, node_id="research"),
        )
        self.assertEqual(session.config.chat_model_config, _model())
        self.assertEqual(
            run.data.context["_sessions"],
            {"research": session_id},
        )

    async def test_agent_retry_reuses_session(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", _model())
        runner = _FakeRunner(self.storage)
        node = WorkflowNode(id="research", config=AgentNodeConfig(
            agent_id=agent_id,
        ))
        run = _run([node])
        handlers = self._handlers(runner)

        await handlers.handle(USER, run, node)
        await handlers.handle(USER, run, node)

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[0][1], runner.calls[1][1])
        self.assertEqual(
            list(run.data.context["_sessions"]),
            ["research"],
        )

    async def test_agent_model_falls_back_to_run_default(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", None)
        runner = _FakeRunner(self.storage)
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id=agent_id,
        ))
        run = _run([node], model=_model("run-m"))

        await self._handlers(runner).handle(USER, run, node)

        session_id = run.data.context["_sessions"]["draft"]
        session = await self.storage.get_session(
            USER,
            agent_id,
            session_id,
        )
        assert session is not None  # for the type checker
        self.assertEqual(session.config.chat_model_config, _model("run-m"))

    async def test_agent_without_model_fails(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", None)
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id=agent_id,
        ))
        run = _run([node])

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(
                _FakeRunner(self.storage),
            ).handle(USER, run, node)

        self.assertIn("'draft'", str(ctx.exception))
        self.assertIn("model", str(ctx.exception))

    async def test_agent_deleted_fails(self) -> None:
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id="ghost",
        ))
        run = _run([node])

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(
                _FakeRunner(self.storage),
            ).handle(USER, run, node)

        self.assertIn("no longer exists", str(ctx.exception))

    async def test_agent_reply_structured_passthrough(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", _model())
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id=agent_id,
        ))

        output = await self._handlers(
            _FakeRunner(self.storage, structured={"k": 1}),
        ).handle(USER, _run([node]), node)

        self.assertEqual(
            output,
            {"text": "canned", "structured": {"k": 1}},
        )

    async def test_agent_silent_turn_fails(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", _model())
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id=agent_id,
        ))

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(_SilentRunner()).handle(
                USER,
                _run([node]),
                node,
            )

        self.assertIn("without a reply", str(ctx.exception))

    async def test_read_reply_rejects_error_bearing_reply(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", _model())
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id=agent_id,
        ))

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(
                _FailedTurnRunner(
                    self.storage,
                    ReplyFinishedReason.ERROR,
                    ErrorInfo(type=ErrorType.UPSTREAM, message="boom"),
                ),
            ).handle(USER, _run([node]), node)

        self.assertIn("turn failed", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    async def test_read_reply_rejects_interrupted_reply(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", _model())
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id=agent_id,
        ))

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(
                _FailedTurnRunner(
                    self.storage,
                    ReplyFinishedReason.INTERRUPTED,
                ),
            ).handle(USER, _run([node]), node)

        self.assertIn("interrupted", str(ctx.exception))

    async def test_unexpected_error_becomes_node_failure(self) -> None:
        agent_id = await _seed_agent(self.storage, "writer", _model())
        node = WorkflowNode(id="draft", config=AgentNodeConfig(
            agent_id=agent_id,
        ))

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(_RaisingRunner()).handle(
                USER,
                _run([node]),
                node,
            )

        self.assertIn("storage exploded", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    async def test_condition_rule_match_and_miss(self) -> None:
        node = WorkflowNode(
            id="gate",
            config=ConditionNodeConfig(
                branches=[
                    ConditionBranch(
                        label="go",
                        rule=(
                            "nodes.check.output.structured.passed == true"
                        ),
                    ),
                    ConditionBranch(label="slow", rule="llm_judge: pick"),
                ],
            ),
        )
        handlers = self._handlers(_FakeRunner(self.storage, "slow"))

        passed = _run(
            [node],
            model=_model(),
            context={"check": {"structured": {"passed": True}}},
        )
        self.assertEqual(
            await handlers.handle(USER, passed, node),
            {"branch": "go"},
        )
        self.assertEqual(self.judge.calls, [])

        failed = _run(
            [node],
            model=_model(),
            context={"check": {"structured": {"passed": False}}},
        )
        self.assertEqual(
            await handlers.handle(USER, failed, node),
            {"branch": "slow"},
        )
        self.assertEqual(len(self.judge.calls), 1)

    async def test_condition_literal_forms(self) -> None:
        self.assertEqual(
            await self._condition(
                "nodes.a.output.text == ready",
                {"a": {"text": "Ready"}},
            ),
            "hit",
        )
        self.assertEqual(
            await self._condition(
                'nodes.a.output.text == "Ready"',
                {"a": {"text": "Ready"}},
            ),
            "hit",
        )
        self.assertEqual(
            await self._condition(
                'nodes.a.output.text == "ready"',
                {"a": {"text": "Ready"}},
            ),
            "miss",
        )
        self.assertEqual(
            await self._condition(
                "nodes.a.output.structured.count == 3",
                {"a": {"structured": {"count": 3}}},
            ),
            "hit",
        )
        self.assertEqual(
            await self._condition(
                "nodes.a.output.structured.count == 4",
                {"a": {"structured": {"count": 3}}},
            ),
            "miss",
        )

    async def test_condition_llm_judge(self) -> None:
        node = WorkflowNode(
            id="gate",
            config=ConditionNodeConfig(
                branches=[
                    ConditionBranch(
                        label="yes",
                        rule="llm_judge: prefer yes when positive",
                    ),
                    ConditionBranch(label="no", rule="llm_judge: else"),
                ],
            ),
        )
        runner = _FakeRunner(self.storage, "yes please")
        run = _run([node], model=_model())

        output = await self._handlers(runner).handle(USER, run, node)

        self.assertEqual(output, {"branch": "yes"})
        self.assertEqual(len(self.judge.calls), 1)
        _, name, system_prompt = self.judge.calls[0]
        self.assertEqual(name, "workflow-branch-judge")
        self.assertIn("yes, no", system_prompt)
        # The specific instruction rides the user message, not the
        # system prompt, so the judge agent stays generic.
        self.assertNotIn("prefer yes", system_prompt)
        user_message = runner.calls[0][3].get_text_content()
        self.assertIn("prefer yes when positive", user_message)
        self.assertIn("Q3 revenue", user_message)
        self.assertIn("gate:judge", run.data.context["_sessions"])

    async def test_condition_llm_judge_garbage_reply_fails(self) -> None:
        node = WorkflowNode(
            id="gate",
            config=ConditionNodeConfig(
                branches=[
                    ConditionBranch(label="yes", rule="llm_judge: pick"),
                    ConditionBranch(label="no", rule="llm_judge: else"),
                ],
            ),
        )
        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(_FakeRunner(self.storage, "banana")).handle(
                USER,
                _run([node], model=_model()),
                node,
            )

        self.assertIn("no branch label", str(ctx.exception))
        self.assertIn("banana", str(ctx.exception))

    async def test_loop_iterate_runs_each_item(self) -> None:
        body_id = await _seed_agent(self.storage, "body", _model())
        node = WorkflowNode(
            id="fan",
            config=LoopNodeConfig(
                mode="iterate",
                list_template="{{input.items}}",
                body_agent_id=body_id,
                item_template="Summarize {{item}}",
            ),
        )
        runner = _FakeRunner(self.storage)
        run = _run(
            [node],
            inputs={"topic": "Q3 revenue", "items": ["a", "b", "c"]},
        )

        output = await self._handlers(runner).handle(USER, run, node)

        self.assertEqual(
            output,
            {
                "items": [{"text": "canned", "structured": None}] * 3,
                "attempts": 3,
            },
        )
        self.assertEqual(
            [call[3].get_text_content() for call in runner.calls],
            ["Summarize a", "Summarize b", "Summarize c"],
        )
        # One conversation per item, keyed by position.
        self.assertEqual(len({call[1] for call in runner.calls}), 3)
        self.assertEqual(
            sorted(run.data.context["_sessions"]),
            ["fan:body:0", "fan:body:1", "fan:body:2"],
        )

    async def test_loop_iterate_rejects_non_list(self) -> None:
        broken = WorkflowNode(
            id="fan",
            config=LoopNodeConfig(
                mode="iterate",
                list_template="not json at all",
                body_agent_id="any",
            ),
        )
        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(_FakeRunner(self.storage)).handle(
                USER,
                _run([broken]),
                broken,
            )
        self.assertIn("valid JSON", str(ctx.exception))

        dicty = WorkflowNode(
            id="fan",
            config=LoopNodeConfig(
                mode="iterate",
                list_template="{{input.payload}}",
                body_agent_id="any",
            ),
        )
        run = _run(
            [dicty],
            inputs={"topic": "t", "payload": {"k": 1}},
        )
        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(_FakeRunner(self.storage)).handle(
                USER,
                run,
                dicty,
            )
        self.assertIn("JSON list", str(ctx.exception))

    async def test_loop_iterate_needs_body_agent(self) -> None:
        node = WorkflowNode(
            id="fan",
            config=LoopNodeConfig(
                mode="iterate",
                list_template="{{input.items}}",
                body_agent_id="",
                item_template="{{item}}",
            ),
        )
        run = _run([node], inputs={"topic": "t", "items": [1]})

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(_FakeRunner(self.storage)).handle(
                USER,
                run,
                node,
            )

        self.assertIn("body agent", str(ctx.exception))

    async def test_loop_retry_until_pass(self) -> None:
        body_id = await _seed_agent(self.storage, "body", _model())
        node = WorkflowNode(
            id="polish",
            config=LoopNodeConfig(
                mode="retry_until",
                body_agent_id=body_id,
                item_template="Draft {{input.topic}}",
                verifier_prompt="must mention revenue",
            ),
        )
        runner = _FakeRunner(self.storage, "draft body", "PASS")
        run = _run([node], model=_model())

        output = await self._handlers(runner).handle(USER, run, node)

        self.assertEqual(
            output,
            {
                "items": [{"text": "draft body", "structured": None}],
                "attempts": 1,
            },
        )
        self.assertEqual(
            [call[3].get_text_content() for call in runner.calls],
            [
                "Draft Q3 revenue",
                "Criteria:\nmust mention revenue\n\n"
                "Work to verify:\ndraft body",
            ],
        )
        self.assertEqual(self.judge.calls[0][1], "workflow-verifier")
        self.assertEqual(
            sorted(run.data.context["_sessions"]),
            ["polish:body", "polish:verifier"],
        )

    async def test_loop_retry_until_fail_raises(self) -> None:
        body_id = await _seed_agent(self.storage, "body", _model())
        node = WorkflowNode(
            id="polish",
            config=LoopNodeConfig(
                mode="retry_until",
                body_agent_id=body_id,
                item_template="Draft {{input.topic}}",
                verifier_prompt="must mention revenue",
            ),
        )

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(
                _FakeRunner(self.storage, "draft body", "FAIL"),
            ).handle(USER, _run([node], model=_model()), node)

        self.assertIn("verifier rejected", str(ctx.exception))
        self.assertIn("draft body", str(ctx.exception))

    async def test_verifier_rejects_substring_hits(self) -> None:
        """``unpassable`` must not read as PASS: word boundaries rule."""
        body_id = await _seed_agent(self.storage, "body", _model())
        node = WorkflowNode(
            id="polish",
            config=LoopNodeConfig(
                mode="retry_until",
                body_agent_id=body_id,
                item_template="Draft {{input.topic}}",
                verifier_prompt="must mention revenue",
            ),
        )

        with self.assertRaises(NodeFailure) as ctx:
            await self._handlers(
                _FakeRunner(self.storage, "draft body", "unpassable"),
            ).handle(USER, _run([node], model=_model()), node)

        self.assertIn("neither PASS nor FAIL", str(ctx.exception))

    async def test_judge_tolerates_formatted_labels(self) -> None:
        """Formatting around a verdict word must not break matching."""
        body_id = await _seed_agent(self.storage, "body", _model())
        node = WorkflowNode(
            id="polish",
            config=LoopNodeConfig(
                mode="retry_until",
                body_agent_id=body_id,
                item_template="Draft {{input.topic}}",
                verifier_prompt="must mention revenue",
            ),
        )

        output = await self._handlers(
            _FakeRunner(self.storage, "draft body", "**PASS**."),
        ).handle(USER, _run([node], model=_model()), node)

        self.assertEqual(
            output,
            {
                "items": [{"text": "draft body", "structured": None}],
                "attempts": 1,
            },
        )

    async def test_approval_parks_the_run(self) -> None:
        node = WorkflowNode(
            id="signoff",
            config=HumanApprovalNodeConfig(approvers=["alice", "bob"]),
        )

        with self.assertRaises(ApprovalPending) as ctx:
            await self._handlers(_FakeRunner(self.storage)).handle(
                USER,
                _run([node]),
                node,
            )

        self.assertEqual(ctx.exception.node_id, "signoff")
        self.assertIn("alice, bob", ctx.exception.question)

    async def test_approval_defaults_to_run_owner(self) -> None:
        node = WorkflowNode(id="signoff", config=HumanApprovalNodeConfig())

        with self.assertRaises(ApprovalPending) as ctx:
            await self._handlers(_FakeRunner(self.storage)).handle(
                USER,
                _run([node]),
                node,
            )

        self.assertEqual(ctx.exception.node_id, "signoff")
        self.assertNotIn("Awaiting approval from", ctx.exception.question)
