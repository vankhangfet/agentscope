# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The WorkflowRun chat tool, constructed directly over real services.

Fakeredis storage plus a hand-built WorkflowService / WorkflowEngine
(the chat runner and judge factory are fakes) — no app is needed, the
tool only talks to those three. The toolkit-registration test at the
bottom exercises the get_toolkit assembly path.
"""
import asyncio
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis

from utils import FakeWorkspaceManager
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app._manager import BackgroundTaskManager, SchedulerManager
from agentscope.app._service import (
    ResourceAccessService,
    WorkflowEngine,
    WorkflowService,
    get_toolkit,
)
from agentscope.app._tool import WorkflowRun
from agentscope.app.access import DenyAllResourceAccessPolicy
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    AgentChatConfig,
    AgentData,
    AgentNodeConfig,
    AgentRecord,
    ChatModelConfig,
    HumanApprovalNodeConfig,
    RedisStorage,
    SessionConfig,
    SessionRecord,
    WorkflowData,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRecord,
)
from agentscope.credential import OllamaCredential
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolResultState
from agentscope.tool import ToolBase, ToolChunk

USER = "user-1"


def make_storage() -> RedisStorage:
    """A RedisStorage instance backed by fakeredis."""
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


async def _seed_agent(
    storage: RedisStorage,
    name: str,
    model: ChatModelConfig | None,
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


async def _seed_workflow(
    storage: RedisStorage,
    name: str,
    *,
    workflow_id: str | None = None,
    node: WorkflowNode | None = None,
) -> WorkflowRecord:
    """Persist a one-node workflow and return the stored record."""
    record = WorkflowRecord(
        **({"id": workflow_id} if workflow_id else {}),
        user_id=USER,
        data=WorkflowData(
            name=name,
            graph=WorkflowGraph(
                nodes=[
                    node
                    or WorkflowNode(
                        id="research",
                        config=AgentNodeConfig(agent_id="any"),
                    ),
                ],
            ),
        ),
    )
    return await storage.upsert_workflow(USER, record)


def _agent_node(agent_id: str, **kwargs: Any) -> WorkflowNode:
    """An agent node referencing *agent_id*."""
    return WorkflowNode(
        id="research",
        config=AgentNodeConfig(agent_id=agent_id, **kwargs),
    )


def _text(chunk: ToolChunk) -> str:
    """The chunk's text content, joined across blocks."""
    return "\n".join(
        block.text for block in chunk.content
        if isinstance(block, TextBlock)
    )


class _FakeRunner:
    """Files a canned assistant reply, mirroring a real chat turn."""

    def __init__(self, storage: RedisStorage, reply: str = "canned") -> None:
        self._storage = storage
        self._reply = reply

    async def __call__(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        msg: Msg,
    ) -> None:
        await self._storage.upsert_message(user_id, session_id, msg)
        await self._storage.upsert_message(
            user_id,
            session_id,
            AssistantMsg(name=agent_id, content=self._reply),
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
    """Returns a canned judge agent id."""

    async def __call__(
        self,
        user_id: str,
        name: str,
        system_prompt: str,
    ) -> str:
        return "judge"


class _ExplodingEngine:
    """Stands in for the engine; its dispatch always blows up."""

    async def dispatch(self, user_id: str, run_id: str) -> None:
        raise RuntimeError("dispatch blew up")


class WorkflowRunToolTest(IsolatedAsyncioTestCase):
    """Resolve, start, dispatch, wait, summarize — and never raise."""

    async def asyncSetUp(self) -> None:
        self.storage = make_storage()
        self.bus = InMemoryMessageBus()
        await self.bus.__aenter__()
        self.runner = _FakeRunner(self.storage, "Revenue is up 12%.")
        self.service = WorkflowService(self.storage)
        self.engine = WorkflowEngine(
            storage=self.storage,
            message_bus=self.bus,
            chat_runner=self.runner,
            judge_agent_factory=_FakeJudgeFactory(),
        )
        self.credential_id = await self.storage.upsert_credential(
            USER,
            OllamaCredential(host="http://localhost:11434"),
        )
        self.agent_id = await _seed_agent(
            self.storage,
            "writer",
            _model(self.credential_id),
        )

    async def asyncTearDown(self) -> None:
        tasks = list(self.engine._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.bus.aclose()

    def _tool(self, engine: Any = None) -> WorkflowRun:
        """The tool under test, over the real services."""
        return WorkflowRun(
            user_id=USER,
            storage=self.storage,
            workflow_service=self.service,
            workflow_engine=engine or self.engine,
        )

    async def _latest_run(self) -> Any:
        """The newest persisted run, failing loudly when there is none."""
        runs = await self.storage.list_workflow_runs(USER)
        self.assertTrue(runs, "expected a persisted run")
        return runs[0]

    async def test_success_summary_includes_nodes_and_text(self) -> None:
        """A run that finishes inside the wait returns a summary with
        the run id, every node's status and the agent output text."""
        workflow = await _seed_workflow(
            self.storage,
            "pipeline",
            node=_agent_node(self.agent_id),
        )

        chunk = await self._tool()(
            workflow_name_or_id="pipeline",
            input={"topic": "ships"},
            wait_seconds=5,
        )

        self.assertEqual(chunk.state, ToolResultState.SUCCESS)
        run = await self._latest_run()
        self.assertEqual(run.data.workflow_id, workflow.id)
        self.assertEqual(run.data.trigger, "chat_tool")
        text = _text(chunk)
        self.assertIn(f"Run {run.id} SUCCEEDED:", text)
        self.assertIn("research=SUCCEEDED", text)
        self.assertIn("Revenue is up 12%.", text)

    async def test_wait_zero_returns_run_id_still_running(self) -> None:
        """wait_seconds=0 does not wait: the chunk carries the run id
        and points at the runs page."""
        await _seed_workflow(
            self.storage,
            "pipeline",
            node=_agent_node(self.agent_id),
        )

        chunk = await self._tool()(
            workflow_name_or_id="pipeline",
            wait_seconds=0,
        )

        self.assertEqual(chunk.state, ToolResultState.SUCCESS)
        run = await self._latest_run()
        self.assertIn("still RUNNING", _text(chunk))
        self.assertIn(run.id, _text(chunk))

    async def test_failed_run_summary_includes_error(self) -> None:
        """A run whose node keeps failing comes back as an error chunk
        carrying the run-level error text."""
        engine = WorkflowEngine(
            storage=self.storage,
            message_bus=self.bus,
            chat_runner=_RaisingRunner(),
            judge_agent_factory=_FakeJudgeFactory(),
        )
        await _seed_workflow(
            self.storage,
            "pipeline",
            node=_agent_node(self.agent_id, max_retries=0),
        )

        chunk = await self._tool(engine=engine)(
            workflow_name_or_id="pipeline",
            wait_seconds=5,
        )

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        text = _text(chunk)
        self.assertIn("FAILED", text)
        self.assertIn("storage exploded", text)

    async def test_parked_run_reports_waiting_human(self) -> None:
        """A run that parks on an approval stops waiting and says so."""
        await _seed_workflow(
            self.storage,
            "signoff",
            node=WorkflowNode(
                id="gate",
                config=HumanApprovalNodeConfig(),
            ),
        )

        chunk = await self._tool()(
            workflow_name_or_id="signoff",
            wait_seconds=5,
        )

        run = await self._latest_run()
        text = _text(chunk)
        self.assertIn("WAITING_HUMAN", text)
        self.assertIn(run.id, text)

    async def test_unknown_name_or_id_is_an_error_chunk(self) -> None:
        """A value matching no id and no name answers with the
        prescribed message, not an exception."""
        await _seed_workflow(
            self.storage,
            "pipeline",
            node=_agent_node(self.agent_id),
        )

        chunk = await self._tool()(workflow_name_or_id="ghost")

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        self.assertIn(
            "No workflow found with id or name 'ghost'",
            _text(chunk),
        )

    async def test_ambiguous_name_lists_candidates(self) -> None:
        """Two workflows sharing the name answer with both candidates."""
        first = await _seed_workflow(
            self.storage,
            "dup",
            node=_agent_node(self.agent_id),
        )
        second = await _seed_workflow(
            self.storage,
            "dup",
            node=_agent_node(self.agent_id),
        )

        chunk = await self._tool()(workflow_name_or_id="dup")

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        text = _text(chunk)
        self.assertIn("Multiple workflows are named 'dup'", text)
        self.assertIn(first.id, text)
        self.assertIn(second.id, text)

    async def test_start_run_problems_rendered(self) -> None:
        """A 422 from start_run lists its problems in the chunk."""
        bare_id = await _seed_agent(self.storage, "bare", None)
        await _seed_workflow(
            self.storage,
            "broken",
            node=_agent_node(bare_id),
        )

        chunk = await self._tool()(workflow_name_or_id="broken")

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        text = _text(chunk)
        self.assertIn("could not be started", text)
        self.assertIn("no model", text)

    async def test_exact_id_beats_same_named_workflow(self) -> None:
        """Resolution tries the exact id first, so a workflow named
        like another's id never shadows it."""
        by_id = await _seed_workflow(
            self.storage,
            "beta",
            workflow_id="wf-alpha",
            node=_agent_node(self.agent_id),
        )
        await _seed_workflow(
            self.storage,
            "wf-alpha",
            node=_agent_node(self.agent_id),
        )

        chunk = await self._tool()(
            workflow_name_or_id="wf-alpha",
            wait_seconds=5,
        )

        self.assertEqual(chunk.state, ToolResultState.SUCCESS)
        run = await self._latest_run()
        self.assertEqual(run.data.workflow_id, by_id.id)
        self.assertEqual(run.data.workflow_name, "beta")

    async def test_long_agent_output_truncated(self) -> None:
        """Agent text is clipped to 500 characters in the summary."""
        self.runner = _FakeRunner(self.storage, "x" * 600)
        self.engine = WorkflowEngine(
            storage=self.storage,
            message_bus=self.bus,
            chat_runner=self.runner,
            judge_agent_factory=_FakeJudgeFactory(),
        )
        await _seed_workflow(
            self.storage,
            "pipeline",
            node=_agent_node(self.agent_id),
        )

        chunk = await self._tool()(
            workflow_name_or_id="pipeline",
            wait_seconds=5,
        )

        text = _text(chunk)
        self.assertIn("x" * 500, text)
        self.assertNotIn("x" * 600, text)
        self.assertIn("truncated", text)

    async def test_internal_failure_is_an_error_chunk(self) -> None:
        """Whatever blows up inside, the tool answers — never raises."""
        await _seed_workflow(
            self.storage,
            "pipeline",
            node=_agent_node(self.agent_id),
        )

        chunk = await self._tool(engine=_ExplodingEngine())(
            workflow_name_or_id="pipeline",
            wait_seconds=0,
        )

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        self.assertIn("dispatch blew up", _text(chunk))


# ----------------------------------------------------------------------
# Toolkit assembly
# ----------------------------------------------------------------------

class _FakeWorkspace:
    """Stand-in for a resolved workspace — only the three methods
    :func:`get_toolkit` calls are implemented."""

    async def list_tools(self) -> list[ToolBase]:
        """No workspace tools."""
        return []

    async def list_skills(self, agent_id: str) -> list:
        """No workspace skills."""
        return []

    async def list_mcps(self, agent_id: str, session_id: str) -> list:
        """No workspace MCPs."""
        return []


class _NullBus:
    """MessageBus placeholder; nothing assembled here awaits it."""


class _NoOpStorage:
    """Storage placeholder; only list_agents is reached."""

    async def list_agents(self, _user_id: str) -> list:
        """No agents."""
        return []


def _tool_names(toolkit: Any) -> list[str]:
    """Every registered tool name, walking the tool groups."""
    return [t.name for group in toolkit.tool_groups for t in group.tools]


def _make_session(agent_id: str) -> SessionRecord:
    """A minimal session record with a model configured."""
    return SessionRecord(
        user_id=USER,
        agent_id=agent_id,
        config=SessionConfig(
            workspace_id="ws",
            chat_model_config=ChatModelConfig(
                type="dashscope_credential",
                credential_id="c",
                model="m",
                parameters={},
            ),
        ),
    )


class WorkflowRunToolkitTest(IsolatedAsyncioTestCase):
    """get_toolkit attaches caller-passed workflow tools."""

    async def test_workflow_tool_registered(self) -> None:
        """The passed tool lands in the assembled toolkit; None adds
        nothing."""
        storage = make_storage()
        tool = WorkflowRun(
            user_id=USER,
            storage=storage,
            workflow_service=WorkflowService(storage),
            workflow_engine=WorkflowEngine(
                storage=storage,
                message_bus=_NullBus(),  # type: ignore[arg-type]
                chat_runner=_FakeRunner(storage),
                judge_agent_factory=_FakeJudgeFactory(),
            ),
        )
        agent = AgentRecord(
            user_id=USER,
            data=AgentData(
                name="A",
                system_prompt="You are A.",
                context_config=ContextConfig(),
                react_config=ReActConfig(),
            ),
        )
        session = _make_session(agent.id)

        toolkit = await get_toolkit(
            storage=_NoOpStorage(),  # type: ignore[arg-type]
            workspace=_FakeWorkspace(),  # type: ignore[arg-type]
            workspace_manager=FakeWorkspaceManager(),
            scheduler_manager=SchedulerManager(
                storage=_NoOpStorage(),  # type: ignore[arg-type]
                message_bus=_NullBus(),  # type: ignore[arg-type]
                workspace_manager=FakeWorkspaceManager(),
            ),
            background_task_manager=BackgroundTaskManager(
                message_bus=_NullBus(),  # type: ignore[arg-type]
            ),
            message_bus=_NullBus(),  # type: ignore[arg-type]
            middlewares=[],
            user_id=USER,
            agent_record=agent,
            session_record=session,
            resource_access_service=ResourceAccessService(
                storage=_NoOpStorage(),  # type: ignore[arg-type]
                policy=DenyAllResourceAccessPolicy(),
            ),
            workflow_tools=[tool],
        )

        self.assertIn("WorkflowRun", _tool_names(toolkit))
        self.assertIs(await toolkit.get_tool("WorkflowRun"), tool)

        bare = await get_toolkit(
            storage=_NoOpStorage(),  # type: ignore[arg-type]
            workspace=_FakeWorkspace(),  # type: ignore[arg-type]
            workspace_manager=FakeWorkspaceManager(),
            scheduler_manager=SchedulerManager(
                storage=_NoOpStorage(),  # type: ignore[arg-type]
                message_bus=_NullBus(),  # type: ignore[arg-type]
                workspace_manager=FakeWorkspaceManager(),
            ),
            background_task_manager=BackgroundTaskManager(
                message_bus=_NullBus(),  # type: ignore[arg-type]
            ),
            message_bus=_NullBus(),  # type: ignore[arg-type]
            middlewares=[],
            user_id=USER,
            agent_record=agent,
            session_record=session,
            resource_access_service=ResourceAccessService(
                storage=_NoOpStorage(),  # type: ignore[arg-type]
                policy=DenyAllResourceAccessPolicy(),
            ),
        )
        self.assertNotIn("WorkflowRun", _tool_names(bare))
