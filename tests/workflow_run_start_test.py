# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""WorkflowService.start_run: validated run creation with a snapshot."""
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi import HTTPException

from agentscope.app._service import WorkflowService
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
    RunStatus,
    WorkflowData,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRecord,
)
from agentscope.credential import OllamaCredential


def make_storage() -> RedisStorage:
    """Create a RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def _model(credential_id: str) -> ChatModelConfig:
    """A chat model config bound to the given credential."""
    return ChatModelConfig(
        type="ollama",
        credential_id=credential_id,
        model="llama3",
        parameters={},
    )


def _graph(*nodes: WorkflowNode) -> WorkflowGraph:
    """A graph over the given nodes; edges do not matter here."""
    return WorkflowGraph(nodes=list(nodes))


def _agent_node(node_id: str, agent_id: str) -> WorkflowNode:
    """An agent node referencing *agent_id*."""
    return WorkflowNode(
        id=node_id,
        config=AgentNodeConfig(agent_id=agent_id),
    )


class StartRunTest(IsolatedAsyncioTestCase):
    """start_run validates everything dispatch needs, then snapshots.

    Storage is seeded directly (no app): the service under test is the
    only thing these checks exercise.
    """

    async def asyncSetUp(self) -> None:
        self.storage = make_storage()
        self.service = WorkflowService(self.storage)
        self.user_id = "user-1"
        self.credential_id = await self.storage.upsert_credential(
            self.user_id,
            OllamaCredential(host="http://localhost:11434"),
        )

    async def _seed_agent(
        self,
        model: ChatModelConfig | None,
    ) -> str:
        """Upsert an agent template whose model is *model*."""
        return await self.storage.upsert_agent(
            self.user_id,
            AgentRecord(
                user_id=self.user_id,
                data=AgentData(
                    name="worker",
                    chat_config=AgentChatConfig(
                        chat_model_config=model,
                    ),
                ),
            ),
        )

    async def _seed_workflow(self, graph: WorkflowGraph) -> str:
        """Persist a workflow over *graph* and return its id."""
        record = await self.storage.upsert_workflow(
            self.user_id,
            WorkflowRecord(
                user_id=self.user_id,
                data=WorkflowData(name="pipeline", graph=graph),
            ),
        )
        return record.id

    async def _problems(self, **kwargs: object) -> list[str]:
        """Call start_run, assert it answered 422, return problems."""
        with self.assertRaises(HTTPException) as caught:
            await self.service.start_run(self.user_id, **kwargs)
        self.assertEqual(caught.exception.status_code, 422)
        return caught.exception.detail["problems"]

    async def test_run_snapshots_the_graph(self) -> None:
        """A valid run comes back PENDING and keeps its graph even
        after the workflow is edited."""
        agent_id = await self._seed_agent(_model(self.credential_id))
        workflow_id = await self._seed_workflow(
            _graph(_agent_node("a", agent_id)),
        )

        run = await self.service.start_run(
            self.user_id,
            workflow_id,
            input={"topic": "ships"},
        )

        self.assertEqual(run.data.status, RunStatus.PENDING)
        self.assertEqual(run.data.trigger, "manual")
        self.assertEqual(run.data.workflow_id, workflow_id)
        self.assertEqual(run.data.workflow_version, 1)
        self.assertEqual(run.data.workflow_name, "pipeline")
        self.assertEqual(run.data.input, {"topic": "ships"})
        self.assertIsNone(run.data.chat_model_config)

        # Edit the workflow afterwards; the persisted run still walks
        # the graph it started with.
        workflow = await self.storage.get_workflow(
            self.user_id,
            workflow_id,
        )
        workflow.data.graph.nodes.append(_agent_node("b", agent_id))
        await self.storage.upsert_workflow(self.user_id, workflow)

        fetched = await self.storage.get_workflow_run(self.user_id, run.id)
        self.assertIsNotNone(fetched)
        self.assertEqual([n.id for n in fetched.data.graph.nodes], ["a"])

    async def test_run_default_covers_a_modelless_agent(self) -> None:
        """An agent without a model runs on the run-level default."""
        agent_id = await self._seed_agent(None)
        workflow_id = await self._seed_workflow(
            _graph(_agent_node("a", agent_id)),
        )

        default = _model(self.credential_id)
        run = await self.service.start_run(
            self.user_id,
            workflow_id,
            input={},
            chat_model_config=default,
        )

        self.assertEqual(run.data.chat_model_config, default)
        self.assertEqual(run.data.status, RunStatus.PENDING)

    async def test_modelless_agent_without_default_is_422(self) -> None:
        """The problem names the node and both remedies."""
        agent_id = await self._seed_agent(None)
        workflow_id = await self._seed_workflow(
            _graph(_agent_node("a", agent_id)),
        )

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
        )

        self.assertEqual(
            problems,
            [
                "Node 'a' has no model: set one on its agent template "
                "or pass a run-level default.",
            ],
        )

    async def test_unknown_agent_is_422(self) -> None:
        """A graph node referencing a missing agent is rejected."""
        workflow_id = await self._seed_workflow(
            _graph(_agent_node("a", "ghost")),
        )

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
        )

        self.assertEqual(
            problems,
            ["Node 'a' references unknown agent 'ghost'."],
        )

    async def test_unknown_credential_on_agent_model_is_422(self) -> None:
        """An agent model bound to a missing credential is rejected."""
        agent_id = await self._seed_agent(_model("cred-missing"))
        workflow_id = await self._seed_workflow(
            _graph(_agent_node("a", agent_id)),
        )

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
        )

        self.assertEqual(
            problems,
            [
                "Model for node 'a' references unknown credential "
                "'cred-missing'.",
            ],
        )

    async def test_unknown_credential_on_run_default_is_422(self) -> None:
        """A run-level default bound to a missing credential is
        reported as run-level, not per node."""
        agent_id = await self._seed_agent(None)
        workflow_id = await self._seed_workflow(
            _graph(_agent_node("a", agent_id)),
        )

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
            chat_model_config=_model("cred-missing"),
        )

        self.assertEqual(
            problems,
            [
                "Run-level model references unknown credential "
                "'cred-missing'.",
            ],
        )

    async def test_every_problem_is_reported_at_once(self) -> None:
        """Unknown agent and missing model arrive in one 422."""
        bare_id = await self._seed_agent(None)
        workflow_id = await self._seed_workflow(
            _graph(
                _agent_node("a", "ghost"),
                _agent_node("b", bare_id),
            ),
        )

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
        )

        self.assertEqual(len(problems), 2)
        self.assertIn(
            "Node 'a' references unknown agent 'ghost'.",
            problems,
        )
        self.assertIn(
            "Node 'b' has no model: set one on its agent template or "
            "pass a run-level default.",
            problems,
        )

    async def test_missing_workflow_is_404(self) -> None:
        """Starting a run on a missing workflow answers 404."""
        with self.assertRaises(HTTPException) as caught:
            await self.service.start_run(
                self.user_id,
                "wf-missing",
                input={},
            )
        self.assertEqual(caught.exception.status_code, 404)

    async def test_rule_only_condition_and_approval_need_no_model(
        self,
    ) -> None:
        """Rule branches and approval nodes run without any model."""
        graph = _graph(
            WorkflowNode(
                id="route",
                config=ConditionNodeConfig(
                    branches=[
                        ConditionBranch(
                            label="yes",
                            rule="input.ok == true",
                        ),
                        ConditionBranch(label="no", rule="true"),
                    ],
                ),
            ),
            WorkflowNode(id="gate", config=HumanApprovalNodeConfig()),
        )
        workflow_id = await self._seed_workflow(graph)

        run = await self.service.start_run(
            self.user_id,
            workflow_id,
            input={"ok": True},
        )

        self.assertEqual(run.data.status, RunStatus.PENDING)

    async def test_judge_condition_uses_its_own_model(self) -> None:
        """An llm_judge condition resolves the node's model."""
        graph = _graph(
            WorkflowNode(
                id="route",
                config=ConditionNodeConfig(
                    branches=[
                        ConditionBranch(
                            label="yes",
                            rule="llm_judge: is the draft good?",
                        ),
                    ],
                    chat_model_config=_model(self.credential_id),
                ),
            ),
        )
        workflow_id = await self._seed_workflow(graph)

        run = await self.service.start_run(
            self.user_id,
            workflow_id,
            input={},
        )

        self.assertEqual(run.data.status, RunStatus.PENDING)

    async def test_judge_condition_without_model_is_422(self) -> None:
        """An llm_judge condition with no model anywhere is rejected
        with the node-level remedy."""
        graph = _graph(
            WorkflowNode(
                id="route",
                config=ConditionNodeConfig(
                    branches=[
                        ConditionBranch(
                            label="yes",
                            rule="llm_judge: is the draft good?",
                        ),
                    ],
                ),
            ),
        )
        workflow_id = await self._seed_workflow(graph)

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
        )

        self.assertEqual(
            problems,
            [
                "Node 'route' has no model for its llm_judge rule: set "
                "one on the node or pass a run-level default.",
            ],
        )

    async def test_loop_body_agent_resolves_the_run_default(self) -> None:
        """A retry_until loop body runs on the run-level default when
        the body agent has no model of its own."""
        body_id = await self._seed_agent(None)
        graph = _graph(
            WorkflowNode(
                id="fix",
                config=LoopNodeConfig(
                    mode="retry_until",
                    body_agent_id=body_id,
                    verifier_prompt="Is it fixed?",
                ),
            ),
        )
        workflow_id = await self._seed_workflow(graph)

        run = await self.service.start_run(
            self.user_id,
            workflow_id,
            input={},
            chat_model_config=_model(self.credential_id),
        )

        self.assertEqual(run.data.status, RunStatus.PENDING)

    async def test_retry_loop_without_run_default_is_422(self) -> None:
        """A retry_until loop's verifier can only run on the run-level
        model, so a body agent with a model of its own is not enough —
        no default is a statically dead run."""
        body_id = await self._seed_agent(_model(self.credential_id))
        graph = _graph(
            WorkflowNode(
                id="fix",
                config=LoopNodeConfig(
                    mode="retry_until",
                    body_agent_id=body_id,
                    verifier_prompt="Is it fixed?",
                ),
            ),
        )
        workflow_id = await self._seed_workflow(graph)

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
        )

        self.assertEqual(
            problems,
            [
                "Node 'fix' needs a run-level model for its verifier: "
                "pass a run-level default.",
            ],
        )

    async def test_judge_node_model_wins_over_the_run_default(self) -> None:
        """The node's own model is the one checked, not the default:
        the node model's bad credential is reported while the present
        default's valid one passes."""
        graph = _graph(
            WorkflowNode(
                id="route",
                config=ConditionNodeConfig(
                    branches=[
                        ConditionBranch(
                            label="yes",
                            rule="llm_judge: is the draft good?",
                        ),
                    ],
                    chat_model_config=_model("cred-missing"),
                ),
            ),
        )
        workflow_id = await self._seed_workflow(graph)

        problems = await self._problems(
            workflow_id=workflow_id,
            input={},
            chat_model_config=_model(self.credential_id),
        )

        self.assertEqual(
            problems,
            [
                "Model for node 'route' references unknown credential "
                "'cred-missing'.",
            ],
        )
