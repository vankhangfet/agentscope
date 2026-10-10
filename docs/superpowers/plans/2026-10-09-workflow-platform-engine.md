# Workflow Platform — Engine Implementation Plan (Plan 2 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute stored workflows: run records persisted on both backends, a frontier DAG scheduler with four node handlers (agent / condition / loop / human approval), park-and-resume semantics, retries, crash recovery, and the run/approve/cancel/retry REST endpoints.

**Architecture:** The engine is an async orchestrator over persisted state. Every node-state transition is persisted BEFORE dispatch continues, so the frontier loop is a pure function of stored state — a crashed run is resumed by simply respawning the loop. Agent turns reuse `ChatService.run` (the same path SOP steps use); verdict-style LLM calls (condition `llm_judge`, loop verifier) reuse the same machinery through a dedicated judge agent. Node status events are published on the message bus for the future SSE UI.

**Tech Stack:** Python 3.11, asyncio, FastAPI, Pydantic v2, Redis (fakeredis in tests), SQLAlchemy async + Alembic (migration 0006), pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-workflow-platform-design.md` (sections 5, 6, 7, 9)
**Prior plan:** `docs/superpowers/plans/2026-10-08-workflow-platform-backend-foundation.md` — its "recorded debt" items are folded in here: credential existence validated at run start (Task 6), no `fallback_chat_model_config` assumption, `_WorkflowStorageContractMixin` reuse (Task 3), deleted agent ⇒ node failure via retry budget (Task 6), own-only run listing.

**All code, identifiers, comments in English.**

**Run tests:** `.venv/Scripts/python.exe -m pytest tests/<file> -v` from repo root (never bare `python`).

**Branch:** `workflow-engine` (created from `main`).

---

## File Structure

| File | Responsibility |
|---|---|
| Create `src/agentscope/app/storage/_model/_workflow_run.py` | `WorkflowRunData`/`WorkflowRunRecord`/`NodeRunState`/`RunStatus` |
| Create `src/agentscope/app/_service/_workflow_template.py` | Pure mustache-lite renderer `render_template` |
| Create `src/agentscope/app/_service/_workflow_planner.py` | Pure frontier computation `ready_nodes` / `run_is_finished` |
| Modify `src/agentscope/app/storage/_base.py` + `_redis_storage.py` + `_sql/*` + migration `0006_workflow_runs.py` | Run persistence |
| Modify `src/agentscope/app/storage/_model/_session.py` | `WorkflowOrigin` in the SessionOrigin union |
| Create `src/agentscope/app/_service/_workflow_nodes.py` | The four node handlers |
| Create `src/agentscope/app/_service/_workflow_engine.py` | `WorkflowEngine` orchestrator (frontier loop, park/resume, retries, recovery) |
| Modify `src/agentscope/app/_service/_workflow.py` | Run-creation validation (credential existence) |
| Modify `src/agentscope/app/_service/__init__.py`, `_lifespan.py`, `deps.py` | Wiring + startup recovery sweep |
| Modify `src/agentscope/app/_router/_schema/_workflow.py` + `_router/_workflow.py` | Run endpoints |
| Create `tests/workflow_run_models_test.py`, `tests/workflow_template_test.py`, `tests/workflow_planner_test.py`, `tests/workflow_run_storage_test.py`, `tests/workflow_nodes_test.py`, `tests/workflow_engine_test.py`, `tests/app_workflow_run_router_test.py` | Test files |

---

### Task 1: Run data models

**Files:**
- Create: `src/agentscope/app/storage/_model/_workflow_run.py`
- Modify: `src/agentscope/app/storage/_model/__init__.py`, `src/agentscope/app/storage/__init__.py`
- Test: `tests/workflow_run_models_test.py`

- [ ] **Step 1: Write the failing tests**

```python
# -*- coding: utf-8 -*-
"""Run record models: the persisted state one workflow run walks
through. Pure pydantic, no I/O."""
import json
from unittest import TestCase

from agentscope.app.storage import (
    NodeRunState,
    RunStatus,
    WorkflowGraph,
    WorkflowNode,
    AgentNodeConfig,
    WorkflowRunData,
    WorkflowRunRecord,
)


def _graph() -> WorkflowGraph:
    return WorkflowGraph(
        nodes=[
            WorkflowNode(id="a", config=AgentNodeConfig(agent_id="ag1")),
        ],
    )


class WorkflowRunModelsTest(TestCase):
    """Shapes, defaults and JSON round-trips."""

    def test_status_values(self) -> None:
        self.assertEqual(
            {s.value for s in RunStatus},
            {
                "PENDING",
                "RUNNING",
                "WAITING_HUMAN",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            },
        )

    def test_node_state_defaults(self) -> None:
        state = NodeRunState()
        self.assertEqual(state.status, "PENDING")
        self.assertEqual(state.attempts, 0)
        self.assertIsNone(state.output)
        self.assertIsNone(state.error)

    def test_run_data_holds_a_graph_snapshot(self) -> None:
        data = WorkflowRunData(
            workflow_id="wf-1",
            workflow_version=3,
            workflow_name="pipeline",
            graph=_graph(),
            input={"topic": "q3"},
        )
        self.assertEqual(data.status, RunStatus.PENDING)
        self.assertEqual(data.trigger, "manual")
        self.assertEqual(data.context, {})
        self.assertEqual(data.node_states, {})

    def test_run_record_round_trips_through_json(self) -> None:
        record = WorkflowRunRecord(
            user_id="u1",
            data=WorkflowRunData(
                workflow_id="wf-1",
                workflow_version=1,
                workflow_name="p",
                graph=_graph(),
                input={},
                status=RunStatus.WAITING_HUMAN,
                node_states={
                    "a": NodeRunState(
                        status="WAITING_HUMAN",
                        attempts=1,
                        output={"text": "draft"},
                    ),
                },
            ),
        )
        restored = WorkflowRunRecord.model_validate_json(
            record.model_dump_json(),
        )
        self.assertEqual(restored, record)
        self.assertEqual(
            restored.data.node_states["a"].output,
            {"text": "draft"},
        )
```

- [ ] **Step 2: Run → FAIL (ImportError)**

- [ ] **Step 3: Implement** `src/agentscope/app/storage/_model/_workflow_run.py`:

```python
# -*- coding: utf-8 -*-
"""The workflow run models: a snapshot of the graph plus the state of
each node as the run walks it.

The graph is copied into the run at start time — later edits to the
workflow never affect an in-flight run.
"""
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ._base import _RecordBase
from ._session import ChatModelConfig
from ._workflow import WorkflowGraph


class RunStatus(str, Enum):
    """Where a workflow run stands."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeRunState(BaseModel):
    """One node's progress inside a run."""

    status: RunStatus = RunStatus.PENDING
    """The node's own lifecycle (PENDING..SUCCEEDED/FAILED/CANCELLED)."""

    attempts: int = Field(default=0, ge=0, description="Dispatches so far.")

    output: dict[str, Any] | None = None
    """What the node wrote to the blackboard, if it finished."""

    error: str | None = None
    """The failure text when status is FAILED."""


class WorkflowRunData(BaseModel):
    """A run's persisted content."""

    workflow_id: str
    workflow_version: int
    workflow_name: str
    graph: WorkflowGraph
    input: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.PENDING
    chat_model_config: ChatModelConfig | None = None
    """Run-level default model for agent nodes without one."""

    trigger: str = Field(
        default="manual",
        description="manual | schedule | api | chat_tool",
    )

    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Blackboard: node id → output dict.",
    )

    node_states: dict[str, NodeRunState] = Field(
        default_factory=dict,
        description="Node id → progress.",
    )

    error: str | None = None
    """Why the whole run failed, when it did."""

    finished_at: str | None = None
    """ISO timestamp set once terminal."""


class WorkflowRunRecord(_RecordBase):
    """A stored workflow run."""

    user_id: str
    """The owner's user id."""

    data: WorkflowRunData
    """The run's content."""
```

Export `NodeRunState`, `RunStatus`, `WorkflowRunData`, `WorkflowRunRecord` from both `__init__.py` files (mirror how Task 1 of Plan 1 exported the workflow models).

- [ ] **Step 4: Run → 4/4 PASS. Commit `feat(workflow): run record models`.**

---

### Task 2: Template renderer

**Files:**
- Create: `src/agentscope/app/_service/_workflow_template.py`
- Test: `tests/workflow_template_test.py`

- [ ] **Step 1: Failing tests:**

```python
# -*- coding: utf-8 -*-
"""The mustache-lite renderer over the run blackboard."""
from unittest import TestCase

from agentscope.app._service._workflow_template import (
    RenderError,
    render_template,
)


CONTEXT = {
    "input": {"topic": "Q3 revenue"},
    "nodes": {
        "research": {"output": {"text": "found 12 sources"}},
        "check": {"output": {"structured": {"passed": True}}},
    },
}


class RenderTemplateTest(TestCase):
    """Substitution, resolution and errors."""

    def test_plain_text_passes_through(self) -> None:
        self.assertEqual(render_template("no tags", CONTEXT), "no tags")

    def test_input_paths(self) -> None:
        self.assertEqual(
            render_template("Report on {{input.topic}}", CONTEXT),
            "Report on Q3 revenue",
        )

    def test_node_output_paths(self) -> None:
        self.assertEqual(
            render_template(
                "Sources: {{nodes.research.output.text}}",
                CONTEXT,
            ),
            "Sources: found 12 sources",
        )

    def test_nested_structured_paths(self) -> None:
        self.assertEqual(
            render_template("{{nodes.check.output.structured.passed}}",
                            CONTEXT),
            "true",
        )

    def test_item_placeholder(self) -> None:
        self.assertEqual(
            render_template("{{item}}", {"item": "apple"}),
            "apple",
        )

    def test_missing_path_raises_render_error(self) -> None:
        with self.assertRaises(RenderError) as ctx:
            render_template("{{nodes.ghost.output.text}}", CONTEXT)
        self.assertIn("nodes.ghost", str(ctx.exception))

    [ ] plus: `test_none_renders_as_empty` (a None value renders ""),
    `test_json_tag_renders_structured` — a tag `{{nodes.research.output}}`
    whose resolved value is a dict renders as compact JSON
    (`json.dumps(value, ensure_ascii=False)`).
```

(write those last two tests fully — same style)

- [ ] **Step 2: FAIL (ImportError) → Step 3: Implement:**

```python
# -*- coding: utf-8 -*-
"""Mustache-style rendering of node inputs over the run blackboard."""
import json
import re
from typing import Any


class RenderError(ValueError):
    """A template referenced a path the blackboard does not hold."""


_TAG = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")


def render_template(template: str, context: dict[str, Any]) -> str:
    """Substitute ``{{dotted.path}}`` tags from ``context``.

    Args:
        template (`str`): The template text.
        context (`dict`): The blackboard (``input``, ``nodes``, ``item``).

    Returns:
        `str`: The rendered text.

    Raises:
        `RenderError`: A tag resolves to nothing in ``context``.
    """

    def _resolve(match: re.Match[str]) -> str:
        path = match.group(1)
        value: Any = context
        walked: list[str] = []
        for part in path.split("."):
            walked.append(part)
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise RenderError(
                    f"Template path '{{{{{'.'.join(walked)}}}}}' "
                    "cannot be resolved from the run context.",
                )
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return _TAG.sub(_resolve, template)
```

- [ ] **Step 4: PASS. Commit `feat(workflow): blackboard template renderer`.**

---

### Task 3: Run storage — both backends + migration

**Files:**
- Modify: `src/agentscope/app/storage/_base.py` (abstract block after Workflows)
- Modify: `src/agentscope/app/storage/_redis_storage.py` (keys + methods, mirroring the SOP-run block which indexes runs under both user and sop)
- Modify: `src/agentscope/app/storage/_sql/_tables.py` (`WorkflowRunRow`), `_sql/_storage.py` (methods)
- Create: `src/agentscope/app/storage/_sql/_alembic/versions/0006_workflow_runs.py`
- Test: `tests/workflow_run_storage_test.py`

**Storage contract (implement on both backends):**

```python
    async def upsert_workflow_run(self, user_id: str, record: WorkflowRunRecord) -> WorkflowRunRecord
    async def get_workflow_run(self, user_id: str, run_id: str) -> WorkflowRunRecord | None
    async def list_workflow_runs(self, user_id: str, workflow_id: str | None = None) -> list[WorkflowRunRecord]
        # newest first; SQL filters WHERE, Redis filters in Python
    async def delete_workflow_run(self, user_id: str, run_id: str) -> bool
        # also called by delete_workflow? NO — keep run deletion explicit;
        # workflow deletion leaves orphan runs readable by id (documented).
    async def list_stale_workflow_runs(self, user_id: str) -> list[WorkflowRunRecord]
        # status == RUNNING (recovery sweep helper); SQL: WHERE status,
        # Redis: index scan. Per-user only — the sweep iterates users of
        # running runs via a second index key
        # ``workflow_run_active_index`` (sadd on RUNNING, srem on terminal).
```

Redis keys (add to KeyConfig):

```python
    workflow_run: str = "agentscope:user:{user_id}:workflow_run:{run_id}"
    workflow_run_index: str = "agentscope:user:{user_id}:workflow_runs"
    workflow_run_wf_index: str = (
        "agentscope:user:{user_id}:workflow:{workflow_id}:runs"
    )
    workflow_run_active_index: str = "agentscope:workflow_runs:active"
    # ^ global (no user segment): entries are "user_id:run_id" so the
    # startup sweep finds them without iterating users.
```

SQL: `WorkflowRunRow(_JsonRecordMixin)` — `__tablename__ = "workflow_runs"`, columns `user_id`, `workflow_id`, `status` (all indexed via `_indexed_fields = ("user_id", "workflow_id", "status")` + mapped columns like `SOPRunRow` does for `phase`). Migration `0006_workflow_runs` mirrors `0005` with the three extra indexed columns (copy `0004_sops.py`'s `sop_runs` shape, which has indexed extra columns).

**Tests** (`tests/workflow_run_storage_test.py`) — reuse the contract-mixin pattern from `tests/workflow_storage_test.py` (`_WorkflowStorageContractMixin` there is graph-record specific; create `_WorkflowRunStorageContractMixin` in the new file with the same structure — `_make_storage()` per backend class):

```python
class _WorkflowRunStorageContractMixin:
    """Backend-agnostic workflow-run storage behaviours."""

    async def _make_storage(self) -> StorageBase: ...

    # tests:
    # test_run_round_trip (upsert → get: status/graph snapshot intact)
    # test_list_runs_newest_first (3 runs, explicit created_at shuffle —
    #   mirror the ordering test from workflow_storage_test.py)
    # test_list_runs_filters_by_workflow (2 workflows × 2 runs)
    # test_active_index_tracks_lifecycle (upsert RUNNING → in active
    #   index; upsert SUCCEEDED → removed; list_stale returns it while
    #   RUNNING only)
    # test_owner_scoping (user-2 get → None, list → [])
    # test_delete (True then False)
```

Write each test fully (mirroring the Plan-1 storage tests' bodies; a `_run_record(status=..., created_at=...)` helper builds records). SQL twin class + `test_migrations_alone_create_the_workflow_runs_table` in `tests/storage_sql_test.py` (copy of the 0005 sibling).

- [ ] **TDD loop → PASS (incl. full `storage_sql_test.py` + `storage_redis_test.py`). Commit `feat(workflow): run storage on both backends with migration`.**

---

### Task 4: WorkflowOrigin + session factory

**Files:**
- Modify: `src/agentscope/app/storage/_model/_session.py` (add to the SessionOrigin union):

```python
class WorkflowOrigin(BaseModel):
    """A session a workflow run opened for one of its agent nodes."""

    type: Literal["workflow"] = "workflow"

    workflow_run_id: str
    """The run that opened it."""

    node_id: str
    """Which node's conversation this is."""


SessionOrigin = Annotated[
    Union[
        UserOrigin,
        ScheduleOrigin,
        ChannelOrigin,
        TeamOrigin,
        SOPOrigin,
        WorkflowOrigin,
    ],
    Field(discriminator="type"),
]
```

(export `WorkflowOrigin` from the storage `__init__.py` files like `SOPOrigin` is.)

- Modify: `src/agentscope/app/_service/_workflow_engine.py` does NOT exist yet — the factory lives in the engine (Task 8). This task ships ONLY the origin model + its round-trip test, because SQL serialises origins in the session payload:

**Test** `tests/workflow_run_models_test.py` — add:

```python
    def test_workflow_origin_round_trips_in_a_session(self) -> None:
        record = SessionRecord(
            user_id="u1",
            agent_id="ag1",
            config=SessionConfig(workspace_id="ws", name="n"),
            state=AgentState(),
            origin=WorkflowOrigin(
                workflow_run_id="run-1",
                node_id="research",
            ),
        )
        restored = SessionRecord.model_validate_json(
            record.model_dump_json(),
        )
        self.assertEqual(restored.origin.type, "workflow")
        self.assertEqual(
            restored.origin.workflow_run_id,
            "run-1",
        )
```

(import `SessionRecord`, `SessionConfig`, `AgentState`, `WorkflowOrigin` from `agentscope.app.storage` — check the real required fields of `SessionRecord`/`SessionConfig` in `_model/_session.py` and adapt the constructor; the assertion is the point, not the literal kwargs. Also check whether `SessionState` vs `AgentState` is the right state class by reading how SOP's `_open_run` constructs it, and mirror that.)

- [ ] **TDD → PASS + existing suites green. Commit `feat(workflow): workflow session origin`.**

---

### Task 5: Frontier planner

**Files:**
- Create: `src/agentscope/app/_service/_workflow_planner.py`
- Test: `tests/workflow_planner_test.py`

- [ ] **Step 1: Failing tests:**

```python
# -*- coding: utf-8 -*-
"""The frontier computation: which nodes may run right now."""
from unittest import TestCase

from agentscope.app._service._workflow_planner import (
    run_is_finished,
    ready_nodes,
)
from agentscope.app.storage import (
    AgentNodeConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    RunStatus,
    NodeRunState,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)


def _agent(node_id: str) -> WorkflowNode:
    return WorkflowNode(id=node_id, config=AgentNodeConfig(agent_id="a"))


def _cond(node_id: str, *labels: str) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        config=ConditionNodeConfig(
            branches=[
                ConditionBranch(label=l, rule="true") for l in labels
            ],
        ),
    )


G = lambda nodes, edges: WorkflowGraph(  # noqa: E731
    nodes=nodes,
    edges=[WorkflowEdge(id=f"e{i}", source=s, target=t,
                        branch=b) for i, (s, t, b) in enumerate(edges)],
)


class ReadyNodesTest(TestCase):
    """Who may dispatch, given node states."""

    def test_roots_are_ready(self) -> None:
        graph = G([_agent("a"), _agent("b")], [])
        self.assertEqual(ready_nodes(graph, {}), {"a", "b"})

    def test_successor_waits_for_source(self) -> None:
        graph = G([_agent("a"), _agent("b")], [("a", "b", None)])
        states = {"a": NodeRunState(status=RunStatus.RUNNING)}
        self.assertEqual(ready_nodes(graph, states), set())
        states = {"a": NodeRunState(status=RunStatus.SUCCEEDED)}
        self.assertEqual(ready_nodes(graph, states), {"b"})

    def test_only_the_taken_branch_unlocks(self) -> None:
        graph = G(
            [_cond("c", "yes", "no"), _agent("y"), _agent("n")],
            [("c", "y", "yes"), ("c", "n", "no")],
        )
        states = {
            "c": NodeRunState(
                status=RunStatus.SUCCEEDED,
                output={"branch": "yes"},
            ),
        }
        self.assertEqual(ready_nodes(graph, states), {"y"})

    def test_condition_without_branch_output_unlocks_nothing(self) -> None:
        graph = G(
            [_cond("c", "yes", "no"), _agent("y")],
            [("c", "y", "yes")],
        )
        states = {"c": NodeRunState(status=RunStatus.SUCCEEDED)}
        self.assertEqual(ready_nodes(graph, states), set())

    def test_failed_source_unlocks_nothing(self) -> None:
        graph = G([_agent("a"), _agent("b")], [("a", "b", None)])
        states = {"a": NodeRunState(status=RunStatus.FAILED)}
        self.assertEqual(ready_nodes(graph, states), set())

    def test_diamond_merges(self) -> None:
        graph = G(
            [_agent("a"), _agent("b"), _agent("c"), _agent("d")],
            [("a", "b", None), ("a", "c", None),
             ("b", "d", None), ("c", "d", None)],
        )
        states = {
            "a": NodeRunState(status=RunStatus.SUCCEEDED),
            "b": NodeRunState(status=RunStatus.SUCCEEDED),
            "c": NodeRunState(status=RunStatus.RUNNING),
        }
        self.assertEqual(ready_nodes(graph, states), set())
        states["c"] = NodeRunState(status=RunStatus.SUCCEEDED)
        self.assertEqual(ready_nodes(graph, states), {"d"})


class RunIsFinishedTest(TestCase):
    """Terminal detection."""

    def test_all_succeeded(self) -> None:
        graph = G([_agent("a")], [])
        states = {"a": NodeRunState(status=RunStatus.SUCCEEDED)}
        self.assertTrue(run_is_finished(graph, states))

    def test_any_failed(self) -> None:
        graph = G([_agent("a"), _agent("b")], [])
        states = {
            "a": NodeRunState(status=RunStatus.SUCCEEDED),
            "b": NodeRunState(status=RunStatus.FAILED),
        }
        self.assertTrue(run_is_finished(graph, states))

    def test_in_flight(self) -> None:
        graph = G([_agent("a")], [])
        self.assertFalse(run_is_finished(graph, {}))
```

- [ ] **Step 2: FAIL → Step 3: Implement:**

```python
# -*- coding: utf-8 -*-
"""Frontier computation over a workflow graph and its node states."""
from ..storage import (
    NodeRunState,
    RunStatus,
    WorkflowGraph,
)


def ready_nodes(
    graph: WorkflowGraph,
    node_states: dict[str, NodeRunState],
) -> set[str]:
    """Which nodes have every incoming edge satisfied.

    An edge is satisfied when its source SUCCEEDED and — when the edge
    carries a branch label — the source's output selected that branch.
    Nodes already terminal or running are never re-selected.

    Args:
        graph (`WorkflowGraph`): The run's graph snapshot.
        node_states (`dict`): Node id → current state.

    Returns:
        `set[str]`: Ids that may dispatch now.
    """
    def _state(node_id: str) -> NodeRunState:
        return node_states.get(node_id, NodeRunState())

    busy = {
        RunStatus.RUNNING,
        RunStatus.WAITING_HUMAN,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
    incoming: dict[str, list] = {n.id: [] for n in graph.nodes}
    for edge in graph.edges:
        incoming[edge.target].append(edge)

    ready: set[str] = set()
    for node in graph.nodes:
        if _state(node.id).status in busy:
            continue
        satisfied = True
        for edge in incoming[node.id]:
            src = _state(edge.source)
            if src.status is not RunStatus.SUCCEEDED:
                satisfied = False
                break
            if edge.branch is not None:
                taken = (src.output or {}).get("branch")
                if taken != edge.branch:
                    satisfied = False
                    break
        if satisfied:
            ready.add(node.id)
    return ready


def run_is_finished(
    graph: WorkflowGraph,
    node_states: dict[str, NodeRunState],
) -> bool:
    """A run is finished when every node is terminal."""
    terminal = {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }
    return all(
        node_states.get(node.id, NodeRunState()).status in terminal
        for node in graph.nodes
    )
```

- [ ] **Step 4: PASS. Commit `feat(workflow): frontier planner`.**

---

### Task 6: Node handlers

**Files:**
- Create: `src/agentscope/app/_service/_workflow_nodes.py`
- Test: `tests/workflow_nodes_test.py`

**Design:** One class `NodeHandlers` with constructor `(storage, chat_runner, judge_agent_factory)`. The engine wires:
- `chat_runner: ChatRunner` — `async def __call__(user_id: str, session_id: str, agent_id: str, input_msg: Msg) -> None` (prod: `ChatService.run`; tests: a fake that appends an `AssistantMsg` via `storage.append_message` or by upserting a session message — check how messages are appended by reading `list_messages`'s writers in `_redis_storage.py` and use the same storage API).
- `judge_agent_factory: async def (user_id, name, system_prompt) -> str` — creates/ensures a judge agent record, returns agent id (prod: `storage.upsert_agent` with a record whose name marks it as engine-owned; tests: return canned id and record prompts).

Each handler signature: `async def handle_X(self, user_id, run: WorkflowRunRecord, node: WorkflowNode) -> dict[str, Any]` (returns the node's blackboard output) and raises `NodeFailure(msg)` on failure. The engine owns retry/persist; handlers do one attempt.

**Agent handler:**

```python
async def handle_agent(
    self,
    user_id: str,
    run: WorkflowRunRecord,
    node: WorkflowNode,
) -> dict[str, Any]:
    """One agent turn: session, rendered prompt, run to completion."""
    cfg = node.config  # AgentNodeConfig
    agent = await self._storage.get_agent(user_id, cfg.agent_id)
    if agent is None:
        # Recorded debt: a deleted template fails the node, and the
        # engine's retry budget applies like any transient failure.
        raise NodeFailure(f"Agent {cfg.agent_id!r} no longer exists.")

    model = agent.data.chat_config.chat_model_config
    if model is None:
        model = run.data.chat_model_config
    if model is None:
        raise NodeFailure(
            f"Node {node.id!r} has no model: set one on the agent "
            "template or pass a run-level default.",
        )

    session_id = await self._ensure_session(
        user_id,
        run,
        node.id,
        cfg.agent_id,
        model,
    )
    prompt = render_template(cfg.prompt_template, _blackboard(run))
    await self._chat_runner(
        user_id,
        session_id,
        cfg.agent_id,
        UserMsg(content=prompt),
    )
    return await self._read_reply(user_id, cfg.agent_id, session_id)
```

`_ensure_session`: mirror SOP's `_open_run` session creation — `upsert_session(user_id=..., agent_id=..., config=SessionConfig(workspace_id=_generate_id(), name=f"{run.data.workflow_name} / {node.id}", chat_model_config=model, fallback_chat_model_config=None), state=<mirror SOP's AgentState construction>, origin=WorkflowOrigin(workflow_run_id=run.id, node_id=node.id))`; cache created session ids in `run.data.context` under `_sessions.{node_id}` so retries reuse the session (persisted ⇒ survives crash).

`_read_reply`: `messages, _ = await storage.list_messages(user_id, session_id, limit=20)`; take the last `AssistantMsg`; return `{"text": <its text>, "structured": <structured_output if present else None>}`. (Check `Msg` API for text extraction — `TextBlock`s in `content`; reuse an existing helper if the codebase has one, else take the concatenated text blocks.)

`_blackboard(run)`: `{"input": run.data.input, "nodes": run.data.context}` (context values ARE the outputs).

**Condition handler:**

```python
async def handle_condition(self, user_id, run, node) -> dict[str, Any]:
    cfg = node.config  # ConditionNodeConfig
    for branch in cfg.branches:
        if branch.rule.startswith("llm_judge:"):
            continue
        if _rule_matches(branch.rule, _blackboard(run)):
            return {"branch": branch.label}
    # llm_judge branches (and the fall-through): ask the judge agent.
    labels = [b.label for b in cfg.branches]
    instruction = next(
        (b.rule[len("llm_judge:"):].strip()
         for b in cfg.branchs if b.rule.startswith("llm_judge:")),
        "Pick the branch that applies.",
    )   # (fix the typo `cfg.branchs` → `cfg.branches` when writing!)
    judge_id = await self._judge_agent_factory(
        user_id,
        name="workflow-branch-judge",
        system_prompt=(
            "You are a routing judge. Reply with EXACTLY one of the "
            f"following labels and nothing else: {', '.join(labels)}.\n"
            f"Instruction: {instruction}"
        ),
    )
    session_id = await self._ensure_session(
        user_id, run, f"{node.id}:judge", judge_id,
        cfg.chat_model_config or run.data.chat_model_config or <fail>,
    )
    await self._chat_runner(
        user_id, session_id, judge_id,
        UserMsg(content=json.dumps(run.data.context
                                   | {"input": run.data.input})),
    )
    reply = await self._read_reply(user_id, judge_id, session_id)
    label = _match_label(reply["text"] or "", labels)
    if label is None:
        raise NodeFailure(
            f"Judge reply matched no branch label: {reply['text']!r}",
        )
    return {"branch": label}
```

`_rule_matches(rule, blackboard) -> bool`: rule format `<dotted.path> == <literal>` where literal is `true|false|null|<int>|<float>|"quoted"|'quoted'|bare word`. Resolve path via the same walking as the renderer (extract a shared `_resolve_path(path, context)` helper — refactor `render_template` to use it; keep `RenderError`). Bare-word comparison is case-insensitive string equality. Return False on unresolvable path (a condition may simply not match).

`_match_label(text, labels) -> str | None`: first label appearing in the reply text (case-insensitive, trimmed).

**Loop handler:**

```python
async def handle_loop(self, user_id, run, node) -> dict[str, Any]:
    cfg = node.config  # LoopNodeConfig
    if cfg.mode == "iterate":
        raw = render_template(cfg.list_template, _blackboard(run))
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NodeFailure(
                f"Loop list_template did not render to JSON: {exc}",
            ) from exc
        if not isinstance(items, list):
            raise NodeFailure("Loop list_template must render to a list.")
        outputs = []
        for item in items:   # sequential (parallel deferred; config flag
                              # parsed but treated as sequential in v1 —
                              # document in the handler docstring)
            prompt = render_template(
                cfg.item_template,
                _blackboard(run) | {"item": item},
            )
            outputs.append(
                await self._run_body_once(
                    user_id, run, node.id, cfg.body_agent_id or
                    <agent per item template resolution>, prompt,
                ),
            )
        return {"items": outputs, "attempts": len(items)}

    # retry_until: engine-level attempt budget drives retries; each
    # handler call is ONE attempt = body run + verifier verdict.
    body = await self._run_body_once(
        user_id, run, node.id, cfg.body_agent_id,
        render_template(cfg.item_template, _blackboard(run)),
    )
    verdict = await self._judge(
        user_id, run, f"{node.id}:verifier",
        system_prompt=(
            "You are a verifier. Reply PASS or FAIL only."
            f"\nCriteria: {cfg.verifier_prompt}"
        ),
        payload=body["text"],
    )
    if verdict != "PASS":
        raise NodeFailure(f"Verifier rejected attempt: {verdict}")
    return {"items": [body], "attempts": 1}
```

(For iterate mode the body agent comes from `cfg.body_agent_id` — require it non-empty for iterate too? The Plan-1 model only requires it for retry_until. Decide: iterate REQUIRES `body_agent_id` as well (the model allows empty for iterate; handler raises NodeFailure if empty and list non-empty) — make this explicit with a check and a clear message. `_run_body_once` = ensure session `{node_id}:{index}` unique per attempt via counter in context, render, chat_runner, read_reply.)

**Human approval handler:**

```python
async def handle_approval(self, user_id, run, node) -> dict[str, Any]:
    cfg = node.config  # HumanApprovalNodeConfig
    if cfg.approvers:
        ...  # v1: single-approver = run owner; cfg.approvers recorded
    raise ApprovalPending(node_id=node.id, question=...)
```

`ApprovalPending` is a control-flow exception the engine catches to park the run (WAITING_HUMAN). Timeout: the engine's sweep (Task 8) checks parked nodes' `updated_at` against `timeout_hours` and applies `timeout_branch`.

**Tests** (`tests/workflow_nodes_test.py`): fakeredis storage via `make_storage()`-style helper + fake `chat_runner` that appends a canned `AssistantMsg` through the same storage API the prod `ChatService.run` uses (find the append path — e.g. `storage.append_message` — by reading `_redis_storage.py`; if the API differs adapt the fake). Cases:
- agent handler happy path (session created with WorkflowOrigin + model from template; prompt rendered with input substitution; output read back)
- agent handler: model falls back template → run default; both None → NodeFailure
- agent handler: deleted agent → NodeFailure
- condition handler: rule true/false selects branch; llm_judge via fake chat_runner reply "yes" → branch yes; judge reply garbage → NodeFailure
- loop iterate: 3-item list → 3 chat calls, outputs collected; template not rendering to list → NodeFailure
- loop retry_until: body + verifier PASS → output; verifier FAIL → NodeFailure (engine retries)
- approval handler: raises ApprovalPending

- [ ] **TDD loop → PASS. Commit `feat(workflow): node handlers for agent, condition, loop and approval`.**

---

### Task 7: WorkflowService — run creation + credential validation

**Files:**
- Modify: `src/agentscope/app/_service/_workflow.py`
- Test: extend `tests/app_workflow_router_test.py`? NO — engine-level; test via `tests/workflow_engine_test.py` (Task 8) and a focused unit test here `tests/workflow_run_start_test.py`.

Add to `WorkflowService`:

```python
    async def start_run(
        self,
        user_id: str,
        workflow_id: str,
        input: dict,
        chat_model_config: ChatModelConfig | None,
        trigger: str = "manual",
    ) -> WorkflowRunRecord:
        """Snapshot the workflow and open a run (not yet dispatched).

        Validates: workflow exists; every agent node's agent exists AND
        resolves to a model (template → run default); every referenced
        credential exists (recorded debt: credential check at run
        start, where models are resolved).
        """
        record = await self.get_workflow(user_id, workflow_id)
        problems: list[str] = []
        for node in record.data.graph.nodes:
            cfg = node.config
            agent_ids = []  # per node type, as in _validate
            ...
            for agent_id in agent_ids:
                agent = await self._storage.get_agent(user_id, agent_id)
                if agent is None:
                    problems.append(
                        f"Node {node.id!r} references unknown agent "
                        f"{agent_id!r}.",
                    )
                    continue
                model = (
                    agent.data.chat_config.chat_model_config
                    if isinstance(cfg, (AgentNodeConfig, LoopNodeConfig))
                    else None
                )
            # resolve the effective model per node and check credential
        # (collect every distinct credential_id used by run-level default
        #  and node/agent models; each must exist via
        #  self._storage.get_credential(user_id, credential_id))
        if problems:
            raise HTTPException(422, detail={"problems": problems})
        run = WorkflowRunRecord(
            user_id=user_id,
            data=WorkflowRunData(
                workflow_id=record.id,
                workflow_version=record.data.version,
                workflow_name=record.data.name,
                graph=record.data.graph,
                input=input,
                chat_model_config=chat_model_config,
                trigger=trigger,
            ),
        )
        return await self._storage.upsert_workflow_run(user_id, run)
```

(Write the full implementation concretely — the task's implementer writes real code for the model/credential resolution per node type; this outline pins the semantics: agent-exists + effective-model-resolves + credential-exists per distinct credential_id, all problems reported at once, 422.)

**Test** (`tests/workflow_run_start_test.py`): fakeredis `make_storage()`-based; seed agent with model (credential c-1 created via `storage.upsert_credential`), start_run → run record with graph snapshot (mutate workflow after → run unaffected); missing credential → 422 listing it; agent without model + no run default → 422; with run default → OK.

- [ ] **TDD → PASS. Commit `feat(workflow): validated run creation with graph snapshot`.**

---

### Task 8: WorkflowEngine orchestrator

**Carry-ins from Task 6's review:** (a) `_ensure_session` creates sessions with bare `AgentState()` (PermissionMode.DEFAULT) — a DEFAULT-mode tool confirmation parks the turn; `_read_reply` now fails error-bearing turns so this surfaces as NodeFailure under the retry budget, but Task 8 should decide (and document) whether workflow agent sessions want a specific permission mode; (b) iterate-loop retries re-run all items — a per-item progress stash (`context[f"_loop:{node_id}"]`) would resume at the failed item; deferred, v1 accepts the cost; (c) reuse `NodeHandlers._blackboard` rather than re-deriving; (d) **dispatch nodes through `NodeHandlers.handle()`** — the unexpected-exception→NodeFailure wrap lives at that single choke point; a per-method table would silently bypass it.

**Files:**
- Create: `src/agentscope/app/_service/_workflow_engine.py`
- Modify: `src/agentscope/app/_service/__init__.py`, `_lifespan.py` (construct engine with real chat runner, `app.state.workflow_engine`), `deps.py` (`get_workflow_engine`)
- Test: `tests/workflow_engine_test.py`

**Class shape:**

```python
class WorkflowEngine:
    """Drives runs: dispatch ready nodes, persist every transition,
    park on approvals, resume after crashes."""

    def __init__(
        self,
        storage: StorageBase,
        message_bus: MessageBus,
        chat_runner: ChatRunner,
        judge_agent_factory: JudgeAgentFactory,
        max_parallel: int = 4,
    ) -> None: ...

    # public API (called by router):
    async def dispatch(self, user_id: str, run_id: str) -> None:
        """Spawn (or re-join) the loop for a run as a background task."""

    async def approve(
        self, user_id: str, run_id: str, node_id: str,
        verdict: str, note: str,
    ) -> WorkflowRunRecord: ...
        # 404 unknown run/node; 409 when node not WAITING_HUMAN;
        # verdict ∈ {approve, reject} else 422; writes node output
        # {verdict, note}, marks node SUCCEEDED, re-dispatches.

    async def cancel(self, user_id: str, run_id: str) -> ...: ...
        # marks run + all non-terminal nodes CANCELLED, cancels task.

    async def retry(self, user_id: str, run_id: str) -> ...: ...
        # only when run FAILED: reset the failed node (and nodes it
        # poisoned — those PENDING behind it stay PENDING; simply reset
        # FAILED node to PENDING, clear error) and re-dispatch.

    async def recover_stale_runs(self) -> int:
        """Startup sweep: RUNNING runs respawn their loop; nodes found
        RUNNING are treated as interrupted attempts (retry if budget
        remains, else FAILED); parked runs past timeout_hours follow
        timeout_branch. Returns runs touched."""

    # internals:
    # _loop(user_id, run_id): while not finished: persist-compute-
    #   dispatch bounded by Semaphore(max_parallel); on empty ready set:
    #   if any WAITING_HUMAN → run WAITING_HUMAN and return; else if
    #   not finished → mark FAILED with "graph stalled" (defensive).
    # _dispatch_node: persist node RUNNING (attempts+1) BEFORE the
    #   await; handler → output into context + node SUCCEEDED (persist,
    #   publish bus event); NodeFailure → if attempts <= max_retries:
    #   back to PENDING (persist, event); else node FAILED + run FAILED
    #   (persist); ApprovalPending → node WAITING_HUMAN (persist, event).
    # _publish(run_id, node_id, status): message_bus.publish(
    #   f"workflow_run:{run_id}", {...}) — fire and forget.
    # Terminal: run status + finished_at set, active index updated.
```

**Node dispatch table**: `{"agent": handlers.handle_agent, "condition": ..., "loop": ..., "human_approval": ...}` keyed by `node.config.type`. Retry budget: `max_retries` for agent nodes (from config), loop nodes use their own `max_attempts` semantics inside the handler (engine-level retries for loop = 0; document), condition/approval: no retries (max_retries=0).

**Wiring** in `_lifespan.py` (after workflow_service):

```python
        from ..message_bus import MessageBusKeys  # if needed
        workflow_engine = WorkflowEngine(
            storage=storage,
            message_bus=message_bus,
            chat_runner=chat_service.run,
            judge_agent_factory=_make_judge_agent(storage),
        )
        app.state.workflow_engine = workflow_engine
        await workflow_engine.recover_stale_runs()
```

`_make_judge_agent`: closure that upserts (once per user) an `AgentRecord` named `"workflow-judge"` with `system_prompt` supplied per call — NOTE the system prompt must be per-session not per-agent; so instead: factory returns agent id of a shared generic judge agent (`"You are a judge..."`) and handlers pass the specific instruction in the user message (adjust Task 6's judge flow accordingly: system prompt generic, instruction embedded in the message — the router test asserts only behavior, so this is an internal detail).

**Tests** (`tests/workflow_engine_test.py`) — fakeredis storage + fake chat_runner (canned replies per session, controllable via a dict the test seeds) + real engine:

- linear 2-agent graph: dispatch → both SUCCEEDED in order, context holds outputs, run SUCCEEDED, bus events published (subscribe before dispatch or assert via bus state)
- condition graph: fake reply selects "yes" → only yes-branch node ran
- retry: fake chat_runner fails twice then succeeds, max_retries=2 → attempts=3, SUCCEEDED; with max_retries=0 → run FAILED with error
- approval: dispatch → run WAITING_HUMAN, node WAITING_HUMAN; approve(verdict="approve") → node output recorded, successor dispatched, run SUCCEEDED; approve on non-parked → 409
- reject branch: approval reject → reject-edge successor runs
- cancel mid-flight → CANCELLED, non-terminal nodes CANCELLED
- retry endpoint: after FAILED, retry → run RUNNING again, succeeds
- recover_stale_runs: hand-craft a RUNNING run with one node RUNNING state → recover → node retried (attempts+1) and run completes (fake runner succeeds)
- timeout: run WAITING_HUMAN with node updated_at older than timeout_hours → recover applies timeout_branch fail → run FAILED

- [ ] **TDD → PASS + `app_sop_router_test.py` green (lifespan boots). Commit `feat(workflow): run engine with park/resume and recovery`.**

---

### Task 9: Run REST endpoints

**Files:**
- Modify: `src/agentscope/app/_router/_schema/_workflow.py` (add: `StartRunRequest{input: dict, chat_model_config: ChatModelConfig | None}`, `StartRunResponse{run_id}`, `ListRunsResponse{runs: list[WorkflowRunRecord], total}`, `RunDetailResponse{data: WorkflowRunData}`, `ApproveRequest{node_id, verdict: Literal["approve","reject"], note: str = ""}`)
- Modify: `src/agentscope/app/_router/_workflow.py`:

```
POST   /workflow/{workflow_id}/run      201 → StartRunResponse (service.start_run + engine.dispatch)
GET    /workflow/runs                   ListRunsResponse (query param workflow_id optional)
GET    /workflow/runs/{run_id}          RunDetailResponse
POST   /workflow/runs/{run_id}/approve  RunDetailResponse (200)
POST   /workflow/runs/{run_id}/cancel   RunDetailResponse
POST   /workflow/runs/{run_id}/retry    RunDetailResponse
```

Route ordering: `/workflow/runs` routes MUST be declared BEFORE `/{workflow_id}` routes (FastAPI matches in declaration order) — declare runs first or use distinct prefixes carefully; the current file has `/{workflow_id}` GET/PATCH/DELETE — add the runs routes above them.

- Test: `tests/app_workflow_run_router_test.py` — full app, fakeredis; **approval-only workflow** (no model calls needed: graph = agent-less — approval node only… approval node has no agent; a graph with a single HumanApprovalNodeConfig + approve/reject edges into two terminal nodes is model-free):

```
- run → 201; GET run → RUNNING then WAITING_HUMAN after dispatch
  (engine dispatch is async — poll GET until WAITING_HUMAN with a
  short timeout loop, max ~2s)
- approve approve → run SUCCEEDED, context carries verdict
- reject → reject successor ran (both terminal nodes exist in graph;
  assert node_states of reject-branch node SUCCEEDED)
- runs list filters by workflow_id
- approve wrong node → 404/409 as designed
- cancel → CANCELLED
- unknown workflow run → 404
```

Plus one engine-through-HTTP smoke with an agent node is NOT possible without a model — skip (engine tests cover it).

- [ ] **TDD → PASS. Commit `feat(workflow): run lifecycle REST endpoints`.**

---

### Task 10: Regression sweep + final checks

- [ ] Run the full battery: `tests/workflow_*` + `tests/agent_model_config_test.py` + `tests/agent_config_grouping_test.py` + `tests/app_workflow_router_test.py` + `tests/app_sop_router_test.py` + `tests/storage_redis_test.py` + `tests/storage_sql_test.py` + `tests/test_e2e_api.py` + `tests/hub_router_test.py` — all green.
- [ ] Line lengths ≤ 79 on every touched file; all defs annotated.
- [ ] Commit any fixups. Final task.

## Follow-up plans

**Engine fast-follow backlog (from Task 8's quality review — none blocking, all recorded):** (a) loop-crash heartbeat / bounded re-dispatch from `_forget_task` (a dead loop stalls its run until restart); (b) `WorkflowEngine.close()` cancelling live loop tasks, wired into lifespan teardown (avoid storage/bus closing underneath loops); (c) judge-agent row-per-boot accumulation — lookup-by-name or prune prior `workflow-*` rows; (d) extract a shared "fail a run on a node" helper (message currently in 3 places incl. recovery); (e) cross-process double-dispatch remains a documented v1 non-goal (single engine process per deployment); (f) SQL `list_active_workflow_runs` per-row poison tolerance — `_to_record` validates eagerly inside the list comprehension, so unknown schema drift in one row still aborts the SQL sweep/boot (Redis path is covered; ~6 lines to mirror it); (g) spec §6's run-list status filter (list currently filters workflow_id only — pick up with the Plan 3 perf notes); (h) spec §5's retryable-exception classes are moot at engine level (ChatService folds model errors into error-bearing replies; the engine retries all NodeFailures under per-type budgets) — document if the semantics ever change; (i) recovery-path FAILED runs leave unreachable PENDING nodes PENDING (normal path cancels them) — display-only skew, converges on retry, belongs with the shared-fail-helper item; (j) 404 noun drift: service "Run" vs engine "Workflow run".

- **Plan 3 — Triggers:** scheduler → engine.dispatch, `run_workflow` chat tool, `POST /credential/test`. **Performance notes from Task 3's review (apply when wiring the hot loop):** (a) wrap the Redis run-upsert's 4-5 sequential round trips in a `pipeline()`; (b) consider splitting the immutable graph snapshot into its own key/column written once at run creation so per-transition upserts shrink; (c) `list_workflow_runs`/`list_active_workflow_runs` are N+1 GETs — `mget` is a drop-in improvement.
- **Plan 4 — Frontend:** React Flow builder, run view (SSE from `workflow_run:{run_id}` bus channel), approvals inbox, agent template form fields (incl. the recorded SchemaForm debt). **Also from Task 4's review:** add `'sop'` and `'workflow'` to the `SessionOrigin` TS type in `examples/web_ui/frontend/src/api/types.ts` (currently lags; generic-icon fallback masks it).
