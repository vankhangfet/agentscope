# Workflow Platform — Backend Foundation Implementation Plan (Plan 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the data foundation of the workflow platform: agent model-config fields, workflow graph models with structural validation, workflow persistence on both storage backends, and a validated CRUD REST API.

**Architecture:** Follows the existing SOP feature's patterns end to end — pydantic records in `storage/_model/`, abstract methods on `StorageBase` implemented by `RedisStorage` and `AsyncSQLAlchemyStorage` (plus an Alembic migration), a service class wired in `_lifespan.py`, and a FastAPI router registered in `_app.py`. Engine, runs, triggers, and frontend are Plans 2–4.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, Redis (via fakeredis in tests), SQLAlchemy 2.0 async + Alembic, pytest (`IsolatedAsyncioTestCase`).

**Spec:** `docs/superpowers/specs/2026-10-08-workflow-platform-design.md`

**All code, identifiers, comments, and UI text in English** (project constraint).

**Spec deviation (approved during planning):** `AgentTemplateRecord` from the spec is NOT created. The service already has `AgentRecord` with full CRUD at `/agent` and a schema-driven UI (`GET /agent/schema/v2`). We extend `AgentData` with optional `chat_model_config` / `tools` / `memory` fields instead — same capability, no duplicate record type.

**How to run tests (Windows, from repo root):**

```bash
python -m pytest tests/workflow_graph_test.py -v
```

---

## File Structure

| File | Responsibility |
|---|---|
| Create `src/agentscope/app/storage/_model/_workflow.py` | Graph models (nodes, edges, node configs) + `validate_workflow_graph` + `WorkflowData`/`WorkflowRecord` |
| Modify `src/agentscope/app/storage/_model/_agent.py` | `AgentData` gains `chat_model_config`, `tools`, `memory` |
| Modify `src/agentscope/app/_router/_schema/_agent.py` | `CreateAgentRequest`/`UpdateAgentRequest` accept the new fields |
| Modify `src/agentscope/app/storage/_base.py` | Abstract `upsert/get/list/delete_workflow` |
| Modify `src/agentscope/app/storage/_redis_storage.py` | KeyConfig keys + Redis implementations |
| Modify `src/agentscope/app/storage/__init__.py` | Export the new models |
| Modify `src/agentscope/app/storage/_sql/_tables.py` | `WorkflowRow` |
| Modify `src/agentscope/app/storage/_sql/_mappers.py` | Record↔row registry entry |
| Modify `src/agentscope/app/storage/_sql/_storage.py` | SQL implementations |
| Create `src/agentscope/app/storage/_sql/_alembic/versions/0005_workflows.py` | Migration |
| Create `src/agentscope/app/_service/_workflow.py` | `WorkflowService` — CRUD + cross-record validation |
| Modify `src/agentscope/app/_lifespan.py`, `src/agentscope/app/deps.py` | Wire the service, `get_workflow_service` |
| Create `src/agentscope/app/_router/_schema/_workflow.py` | Request/response schemas |
| Create `src/agentscope/app/_router/_workflow.py` | REST endpoints |
| Modify `src/agentscope/app/_router/__init__.py`, `src/agentscope/app/_app.py` | Export + include the router |
| Create `tests/workflow_graph_test.py` | Graph validation unit tests |
| Create `tests/agent_model_config_test.py` | AgentData extension round-trip |
| Create `tests/workflow_storage_test.py` | Redis + SQL persistence round-trips |
| Create `tests/app_workflow_router_test.py` | Router integration tests |

---

### Task 1: Workflow graph models and structural validation

**Files:**
- Create: `src/agentscope/app/storage/_model/_workflow.py`
- Test: `tests/workflow_graph_test.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/workflow_graph_test.py`:

```python
# -*- coding: utf-8 -*-
"""Structural validation of workflow graphs: the checks that run before
anything is persisted. Pure functions, no I/O."""
from unittest import TestCase

from agentscope.app.storage import (
    AgentNodeConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    validate_workflow_graph,
)


def _graph(*nodes: WorkflowNode, **edges: WorkflowEdge) -> WorkflowGraph:
    """Assemble a graph from nodes; edges keyword-named by their id."""
    return WorkflowGraph(nodes=list(nodes), edges=list(edges.values()))


def _agent(node_id: str) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        config=AgentNodeConfig(agent_id="agent-1"),
    )


def _e(edge_id: str, source: str, target: str, branch: str | None = None):
    return WorkflowEdge(id=edge_id, source=source, target=target, branch=branch)


class WorkflowGraphValidationTest(TestCase):
    """Every rule of ``validate_workflow_graph``."""

    def test_a_linear_graph_passes(self) -> None:
        graph = _graph(
            _agent("a"),
            _agent("b"),
            edges={"e1": _e("e1", "a", "b")},
        )
        self.assertEqual(validate_workflow_graph(graph), [])

    def test_a_cycle_is_reported_with_its_path(self) -> None:
        graph = _graph(
            _agent("a"),
            _agent("b"),
            _agent("c"),
            edges={
                "e1": _e("e1", "a", "b"),
                "e2": _e("e2", "b", "c"),
                "e3": _e("e3", "c", "b"),
            },
        )
        problems = validate_workflow_graph(graph)
        self.assertEqual(len(problems), 1)
        self.assertIn("Cycle detected", problems[0])
        self.assertIn("b", problems[0])

    def test_self_loop_is_a_cycle(self) -> None:
        graph = _graph(_agent("a"), edges={"e1": _e("e1", "a", "a")})
        problems = validate_workflow_graph(graph)
        self.assertTrue(any("Cycle" in p for p in problems))

    def test_duplicate_node_ids(self) -> None:
        graph = _graph(_agent("a"), _agent("a"))
        self.assertTrue(
            any("Duplicate node ids" in p for p in validate_workflow_graph(graph)),
        )

    def test_edges_must_reference_known_nodes(self) -> None:
        graph = _graph(_agent("a"), edges={"e1": _e("e1", "a", "ghost")})
        problems = validate_workflow_graph(graph)
        self.assertTrue(any("unknown node ghost" in p for p in problems))

    def test_condition_branches_must_have_edges(self) -> None:
        cond = WorkflowNode(
            id="c",
            config=ConditionNodeConfig(
                branches=[
                    ConditionBranch(label="yes", rule="input.ok == true"),
                    ConditionBranch(label="no", rule="input.ok == false"),
                ],
            ),
        )
        graph = _graph(
            _agent("a"),
            cond,
            _agent("yes_node"),
            edges={
                "e1": _e("e1", "a", "c"),
                "e2": _e("e2", "c", "yes_node", branch="yes"),
            },
        )
        problems = validate_workflow_graph(graph)
        self.assertTrue(
            any("no edge for branch 'no'" in p for p in problems),
        )

    def test_condition_edges_cannot_carry_unknown_branches(self) -> None:
        cond = WorkflowNode(
            id="c",
            config=ConditionNodeConfig(
                branches=[ConditionBranch(label="yes", rule="true")],
            ),
        )
        graph = _graph(
            cond,
            _agent("x"),
            edges={"e1": _e("e1", "c", "x", branch="surprise")},
        )
        problems = validate_workflow_graph(graph)
        self.assertTrue(
            any("unknown branch 'surprise'" in p for p in problems),
        )

    def test_condition_node_rejects_unlabeled_outgoing_edge(self) -> None:
        cond = WorkflowNode(
            id="c",
            config=ConditionNodeConfig(
                branches=[ConditionBranch(label="yes", rule="true")],
            ),
        )
        graph = _graph(
            cond,
            _agent("x"),
            edges={
                "e1": _e("e1", "c", "x"),
            },
        )
        problems = validate_workflow_graph(graph)
        self.assertTrue(any("unlabeled outgoing edge" in p for p in problems))

    def test_approval_node_needs_approve_and_reject_edges(self) -> None:
        approval = WorkflowNode(
            id="h",
            config=HumanApprovalNodeConfig(),
        )
        graph = _graph(
            _agent("a"),
            approval,
            _agent("out"),
            edges={
                "e1": _e("e1", "a", "h"),
                "e2": _e("e2", "h", "out", branch="approve"),
            },
        )
        problems = validate_workflow_graph(graph)
        self.assertTrue(any("'reject'" in p for p in problems))

    def test_approval_timeout_branch_needs_an_edge(self) -> None:
        approval = WorkflowNode(
            id="h",
            config=HumanApprovalNodeConfig(timeout_branch="escalate"),
        )
        graph = _graph(
            _agent("a"),
            approval,
            _agent("out"),
            edges={
                "e1": _e("e1", "a", "h"),
                "e2": _e("e2", "h", "out", branch="approve"),
                "e3": _e("e3", "h", "out", branch="reject"),
            },
        )
        problems = validate_workflow_graph(graph)
        self.assertTrue(any("escalate" in p for p in problems))

    def test_iterate_loop_requires_list_template(self) -> None:
        with self.assertRaises(ValueError):
            LoopNodeConfig(mode="iterate", list_template="")

    def test_retry_loop_requires_body_and_verifier(self) -> None:
        with self.assertRaises(ValueError):
            LoopNodeConfig(mode="retry_until", body_agent_id="a")
        with self.assertRaises(ValueError):
            LoopNodeConfig(mode="retry_until", verifier_prompt="good?")

    def test_empty_graph_is_rejected_by_the_schema(self) -> None:
        with self.assertRaises(Exception):
            WorkflowGraph(nodes=[], edges=[])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/workflow_graph_test.py -v
```

Expected: FAIL — `ImportError: cannot import name 'AgentNodeConfig'`.

- [ ] **Step 3: Implement the models**

Create `src/agentscope/app/storage/_model/_workflow.py`:

```python
# -*- coding: utf-8 -*-
"""The workflow storage models: a DAG of nodes a run walks through.

The graph mirrors React Flow's shape (nodes carry canvas ``position``,
edges carry ids) so the builder UI persists what it renders; the engine
reads only ``config`` and ignores presentation fields.
"""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

from ._base import _RecordBase
from ._session import ChatModelConfig


class WorkflowPosition(BaseModel):
    """Where a node sits on the builder canvas."""

    x: float = Field(default=0.0, description="Canvas x coordinate.")
    y: float = Field(default=0.0, description="Canvas y coordinate.")


class AgentNodeConfig(BaseModel):
    """Run one agent to completion."""

    type: Literal["agent"] = "agent"

    agent_id: str = Field(description="The agent this node runs.")

    prompt_template: str = Field(
        default="{{input}}",
        description=(
            "Mustache-style template over the run blackboard, e.g. "
            "``{{nodes.researcher.output.text}}`` or ``{{input.topic}}``."
        ),
        json_schema_extra={"format": "textarea"},
    )

    max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Retries before the node fails the run.",
    )


class ConditionBranch(BaseModel):
    """One arm of a condition node."""

    label: str = Field(description="Matches an outgoing edge's branch.")

    rule: str = Field(
        description=(
            "Either an expression over the blackboard (e.g. "
            "``nodes.check.output.structured.passed == true``) or "
            "``llm_judge: <instruction>`` for a model verdict."
        ),
    )


class ConditionNodeConfig(BaseModel):
    """Pick one outgoing edge by rule or model verdict."""

    type: Literal["condition"] = "condition"

    branches: list[ConditionBranch] = Field(
        min_length=1,
        description="Candidate arms; exactly one is chosen per run.",
    )

    chat_model_config: ChatModelConfig | None = Field(
        default=None,
        description="Model used by ``llm_judge`` rules.",
    )


class LoopNodeConfig(BaseModel):
    """Iterate a list, or retry a body until a verifier passes."""

    type: Literal["loop"] = "loop"

    mode: Literal["iterate", "retry_until"] = Field(
        description=(
            "``iterate`` fans the body out over a list; ``retry_until`` "
            "reruns the body until the verifier passes."
        ),
    )

    list_template: str = Field(
        default="",
        description="``iterate``: template resolving to a JSON list.",
    )

    body_agent_id: str = Field(
        default="",
        description="``retry_until``: the agent rerun on each attempt.",
    )

    item_template: str = Field(
        default="{{item}}",
        description="How a list item is passed to the body agent.",
    )

    verifier_prompt: str = Field(
        default="",
        description="``retry_until``: what the verifier must accept.",
        json_schema_extra={"format": "textarea"},
    )

    max_attempts: int = Field(
        default=3,
        ge=1,
        le=20,
        description="``retry_until``: attempt budget.",
    )

    parallel: bool = Field(
        default=False,
        description="``iterate``: run items concurrently (bounded).",
    )

    @model_validator(mode="after")
    def _check_mode_fields(self) -> "LoopNodeConfig":
        """Each mode requires its own fields."""
        if self.mode == "iterate" and not self.list_template:
            raise ValueError("iterate loops need a list_template")
        if self.mode == "retry_until":
            if not self.body_agent_id:
                raise ValueError("retry_until loops need a body_agent_id")
            if not self.verifier_prompt:
                raise ValueError("retry_until loops need a verifier_prompt")
        return self


class HumanApprovalNodeConfig(BaseModel):
    """Park the run until an approver answers."""

    type: Literal["human_approval"] = "human_approval"

    approvers: list[str] = Field(
        default_factory=list,
        description="User ids asked to verdict; empty means the run owner.",
    )

    timeout_hours: float = Field(
        default=24.0,
        gt=0,
        description="How long the run may sit parked.",
    )

    timeout_branch: str = Field(
        default="fail",
        description=(
            "``fail`` ends the run as FAILED on timeout; any other "
            "value must match an outgoing edge's branch."
        ),
    )


WorkflowNodeConfig = Annotated[
    Union[
        AgentNodeConfig,
        ConditionNodeConfig,
        LoopNodeConfig,
        HumanApprovalNodeConfig,
    ],
    Field(discriminator="type"),
]


class WorkflowNode(BaseModel):
    """One step on the canvas."""

    id: str = Field(description="Unique node id within the workflow.")

    position: WorkflowPosition = Field(
        default_factory=WorkflowPosition,
        description="Canvas coordinates (React Flow).",
    )

    config: WorkflowNodeConfig


class WorkflowEdge(BaseModel):
    """A directed connection between two nodes."""

    id: str = Field(description="Unique edge id within the workflow.")

    source: str = Field(description="Outgoing node id.")

    target: str = Field(description="Incoming node id.")

    branch: str | None = Field(
        default=None,
        description=(
            "Label selecting this edge from a condition or approval "
            "node; None on plain sequence edges."
        ),
    )


class WorkflowGraph(BaseModel):
    """The DAG itself, React Flow-compatible."""

    nodes: list[WorkflowNode] = Field(
        min_length=1,
        description="The workflow's nodes.",
    )

    edges: list[WorkflowEdge] = Field(
        default_factory=list,
        description="The workflow's directed edges.",
    )


def validate_workflow_graph(graph: WorkflowGraph) -> list[str]:
    """Structural checks every workflow must pass before saving.

    Returns a list of human-readable problems; empty means valid.
    Cross-record checks (agent existence) live in the service layer.
    """
    problems: list[str] = []

    node_ids = [node.id for node in graph.nodes]
    duplicates = {i for i in node_ids if node_ids.count(i) > 1}
    if duplicates:
        problems.append(f"Duplicate node ids: {sorted(duplicates)}")
    known = set(node_ids)

    outgoing: dict[str, list[WorkflowEdge]] = {i: [] for i in known}
    for edge in graph.edges:
        if edge.source not in known:
            problems.append(
                f"Edge {edge.id} starts at unknown node {edge.source}",
            )
        if edge.target not in known:
            problems.append(
                f"Edge {edge.id} ends at unknown node {edge.target}",
            )
        if edge.source in known and edge.target in known:
            outgoing[edge.source].append(edge)

    cycle = _find_cycle(known, outgoing)
    if cycle is not None:
        problems.append("Cycle detected: " + " -> ".join(cycle))

    for node in graph.nodes:
        out = outgoing[node.id]
        if isinstance(node.config, ConditionNodeConfig):
            _check_branches(
                node.id,
                out,
                {b.label for b in node.config.branches},
                problems,
                unlabeled_is_error=True,
            )
        elif isinstance(node.config, HumanApprovalNodeConfig):
            labelled = _check_branches(
                node.id,
                out,
                {"approve", "reject"},
                problems,
                unlabeled_is_error=True,
            )
            cfg = node.config
            if (
                cfg.timeout_branch != "fail"
                and cfg.timeout_branch not in labelled
            ):
                problems.append(
                    f"Approval node {node.id} timeout_branch "
                    f"{cfg.timeout_branch!r} has no matching outgoing edge",
                )

    return problems


def _find_cycle(
    known: set[str],
    outgoing: dict[str, list[WorkflowEdge]],
) -> list[str] | None:
    """Iterative coloured DFS; returns the cycle path or None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {i: WHITE for i in known}
    for start in known:
        if colour[start] != WHITE:
            continue
        colour[start] = GRAY
        path = [start]
        stack = [iter(outgoing[start])]
        while stack:
            advanced = False
            for edge in stack[-1]:
                nxt = edge.target
                if colour[nxt] == GRAY:
                    return path[path.index(nxt):] + [nxt]
                if colour[nxt] == WHITE:
                    colour[nxt] = GRAY
                    path.append(nxt)
                    stack.append(iter(outgoing[nxt]))
                    advanced = True
                    break
            if not advanced:
                colour[path.pop()] = BLACK
                stack.pop()
    return None


def _check_branches(
    node_id: str,
    out: list[WorkflowEdge],
    labels: set[str],
    problems: list[str],
    unlabeled_is_error: bool,
) -> set[str]:
    """Match a branching node's labels against its outgoing edges."""
    labelled = {e.branch for e in out if e.branch is not None}
    for missing in labels - labelled:
        problems.append(
            f"Branching node {node_id} has no edge for branch {missing!r}",
        )
    for extra in labelled - labels:
        problems.append(
            f"Edge from {node_id} carries unknown branch {extra!r}",
        )
    if unlabeled_is_error and any(e.branch is None for e in out):
        problems.append(
            f"Branching node {node_id} has an unlabeled outgoing edge",
        )
    return labelled


class WorkflowData(BaseModel):
    """A workflow's editable content."""

    name: str = Field(description="Display name.")

    description: str = Field(default="", description="What it does.")

    graph: WorkflowGraph

    version: int = Field(
        default=1,
        ge=1,
        description="Bumped on each update.",
    )


class WorkflowRecord(_RecordBase):
    """A stored workflow."""

    user_id: str
    """The owner's user id."""

    data: WorkflowData
    """The workflow's content."""
```

Then export from `src/agentscope/app/storage/__init__.py` — add to the import block from `._model` and to `__all__`:

```python
    AgentNodeConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    WorkflowData,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPosition,
    WorkflowRecord,
    validate_workflow_graph,
```

(Also add `from ._model._workflow import validate_workflow_graph` if the import block imports modules rather than names — mirror how `_session.py` names are imported there.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/workflow_graph_test.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_model/_workflow.py src/agentscope/app/storage/__init__.py tests/workflow_graph_test.py
git commit -m "feat(workflow): graph models with structural validation"
```

---

### Task 2: AgentData model-config fields

**Files:**
- Modify: `src/agentscope/app/storage/_model/_agent.py` (class `AgentData`, ~line 65)
- Modify: `src/agentscope/app/_router/_schema/_agent.py` (`CreateAgentRequest`, `UpdateAgentRequest`)
- Test: `tests/agent_model_config_test.py`

- [ ] **Step 1: Write the failing test**

Create `tests/agent_model_config_test.py`:

```python
# -*- coding: utf-8 -*-
"""Agents can carry their own model config, tools and memory flag —
the fields that turn an agent into a reusable template."""
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

HEADERS = {"X-User-ID": "alice"}


def _fake_backends() -> tuple:
    """Build a fakeredis-backed storage and message bus."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _Storage(RedisStorage):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    class _Bus(RedisMessageBus):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    return _Storage(), _Bus()


class AgentModelConfigTest(IsolatedAsyncioTestCase):
    """The template fields round-trip through the agent API."""

    def setUp(self) -> None:
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        self._client = self.enterContext(
            TestClient(
                create_app(
                    storage=storage,
                    message_bus=bus,
                    workspace_manager=LocalWorkspaceManager(workdir),
                    enable_index_worker=False,
                ),
            ),
        )

    def test_agent_carries_model_config_tools_and_memory(self) -> None:
        created = self._client.post(
            "/agent/",
            json={
                "name": "writer",
                "system_prompt": "Write reports.",
                "chat_model_config": {
                    "type": "openai_credential",
                    "credential_id": "c-1",
                    "model": "gpt-4o",
                    "parameters": {"temperature": 0.4},
                },
                "tools": ["web", "files"],
                "memory": True,
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 200)
        agent_id = created.json()["agent_id"]

        fetched = self._client.get(
            f"/agent/{agent_id}",
            headers=HEADERS,
        ).json()
        self.assertEqual(
            fetched["data"]["chat_model_config"]["model"],
            "gpt-4o",
        )
        self.assertEqual(fetched["data"]["tools"], ["web", "files"])
        self.assertTrue(fetched["data"]["memory"])

    def test_new_fields_default_to_none(self) -> None:
        created = self._client.post(
            "/agent/",
            json={"name": "plain"},
            headers=HEADERS,
        ).json()
        fetched = self._client.get(
            f"/agent/{created['agent_id']}",
            headers=HEADERS,
        ).json()
        self.assertIsNone(fetched["data"]["chat_model_config"])
        self.assertEqual(fetched["data"]["tools"], [])
        self.assertFalse(fetched["data"]["memory"])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/agent_model_config_test.py -v
```

Expected: FAIL — the POST body's `chat_model_config` is ignored (response shows `None`) or 422.

- [ ] **Step 3: Implement**

In `src/agentscope/app/storage/_model/_agent.py`, add to `AgentData` (after `invite_config`) and import `ChatModelConfig` from `._session`:

```python
    chat_model_config: ChatModelConfig | None = Field(
        default=None,
        description=(
            "Default model for this agent's sessions; None keeps the "
            "session-level config in charge."
        ),
        title="Chat Model Config",
    )

    tools: list[str] = Field(
        default_factory=list,
        description="Names of workspace MCP tools the agent may use.",
        title="Tools",
    )

    memory: bool = Field(
        default=False,
        description="Enable long-term memory for this agent.",
        title="Memory",
    )
```

In `src/agentscope/app/_router/_schema/_agent.py`, add to `CreateAgentRequest` (after `invite_config`):

```python
    chat_model_config: ChatModelConfig | None = Field(
        default=None,
        description="Default model configuration for this agent.",
    )

    tools: list[str] = Field(
        default_factory=list,
        description="Workspace MCP tool names the agent may use.",
    )

    memory: bool = Field(
        default=False,
        description="Enable long-term memory.",
    )
```

and to `UpdateAgentRequest` (nullable-optional pattern like the others):

```python
    chat_model_config: ChatModelConfig | None = Field(
        default=None,
        description="New default model configuration.",
    )

    tools: list[str] | None = Field(
        default=None,
        description="New tool list.",
    )

    memory: bool | None = Field(
        default=None,
        description="Enable or disable long-term memory.",
    )
```

Import `ChatModelConfig` from `...storage` in that schema file. Finally check the PATCH handler in `src/agentscope/app/_router/_agent.py` applies the new optional fields the same way it applies `name`/`system_prompt` (set-if-not-None); mirror those lines for the three fields.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/agent_model_config_test.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_model/_agent.py src/agentscope/app/_router/_schema/_agent.py src/agentscope/app/_router/_agent.py tests/agent_model_config_test.py
git commit -m "feat(agent): optional model config, tools and memory on agents"
```

---

### Task 3: Workflow storage — StorageBase + Redis

**Files:**
- Modify: `src/agentscope/app/storage/_base.py` (add abstract methods next to the SOP block)
- Modify: `src/agentscope/app/storage/_redis_storage.py` (KeyConfig + methods, mirror the SOP block ~line 1573)
- Test: `tests/workflow_storage_test.py`

- [ ] **Step 1: Write the failing test**

Create `tests/workflow_storage_test.py`:

```python
# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Workflow record persistence on both storage backends."""
from contextlib import AsyncExitStack
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis

from agentscope.app.storage import (
    AgentNodeConfig,
    AsyncSQLAlchemyStorage,
    RedisStorage,
    WorkflowData,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRecord,
)


def make_storage() -> RedisStorage:
    """Create a RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def _workflow_record() -> WorkflowRecord:
    """A one-node workflow record."""
    return WorkflowRecord(
        user_id="user-1",
        data=WorkflowData(
            name="ship",
            description="do the thing",
            graph=WorkflowGraph(
                nodes=[
                    WorkflowNode(
                        id="a",
                        config=AgentNodeConfig(agent_id="agent-1"),
                    ),
                ],
            ),
        ),
    )


class RedisWorkflowStorageTest(IsolatedAsyncioTestCase):
    """Upsert / get / list / delete over fakeredis."""

    async def test_workflow_round_trip(self) -> None:
        storage = make_storage()
        record = _workflow_record()
        saved = await storage.upsert_workflow("user-1", record)
        self.assertEqual(saved.data.name, "ship")

        fetched = await storage.get_workflow("user-1", saved.id)
        self.assertEqual(fetched.data.graph.nodes[0].id, "a")

        listed = await storage.list_workflows("user-1")
        self.assertEqual([r.id for r in listed], [saved.id])

        other = await storage.get_workflow("user-2", saved.id)
        self.assertIsNone(other)

        self.assertTrue(await storage.delete_workflow("user-1", saved.id))
        self.assertFalse(await storage.delete_workflow("user-1", saved.id))
        self.assertEqual(await storage.list_workflows("user-1"), [])


class SqlWorkflowStorageTest(IsolatedAsyncioTestCase):
    """Same behaviours over in-memory SQLite."""

    async def test_workflow_round_trip(self) -> None:
        self._stack = AsyncExitStack()
        storage = await self._stack.enter_async_context(
            AsyncSQLAlchemyStorage(
                "sqlite+aiosqlite:///:memory:",
                create_tables=True,
            ),
        )
        try:
            record = _workflow_record()
            saved = await storage.upsert_workflow("user-1", record)

            fetched = await storage.get_workflow("user-1", saved.id)
            self.assertEqual(fetched.data.name, "ship")

            listed = await storage.list_workflows("user-1")
            self.assertEqual(len(listed), 1)

            self.assertIsNone(
                await storage.get_workflow("user-2", saved.id),
            )

            self.assertTrue(
                await storage.delete_workflow("user-1", saved.id),
            )
            self.assertEqual(await storage.list_workflows("user-1"), [])
        finally:
            await self._stack.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/workflow_storage_test.py -v
```

Expected: FAIL — `AttributeError: upsert_workflow` (abstract missing / not implemented).

- [ ] **Step 3: Implement**

In `src/agentscope/app/storage/_base.py`, next to the SOP abstract block, add (import `WorkflowRecord` from `._model` at the top the same way `SOPRecord` is imported):

```python
    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------

    @abstractmethod
    async def upsert_workflow(
        self,
        user_id: str,
        record: WorkflowRecord,
    ) -> WorkflowRecord:
        """Persist a workflow (create or overwrite)."""

    @abstractmethod
    async def get_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> WorkflowRecord | None:
        """Fetch one workflow by id, or None when absent."""

    @abstractmethod
    async def list_workflows(
        self,
        user_id: str,
    ) -> list[WorkflowRecord]:
        """Every workflow the user owns."""

    @abstractmethod
    async def delete_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> bool:
        """Delete a workflow; True when it existed."""
```

In `src/agentscope/app/storage/_redis_storage.py`:

1. Add to `KeyConfig` (next to `sop`/`sop_index`):

```python
    workflow: str = "agentscope:user:{user_id}:workflow:{workflow_id}"
    workflow_index: str = "agentscope:user:{user_id}:workflows"
```

2. Add a "Workflow persistence" block mirroring the SOP one:

```python
    # ------------------------------------------------------------------
    # Workflow persistence
    # ------------------------------------------------------------------

    async def upsert_workflow(
        self,
        user_id: str,
        record: WorkflowRecord,
    ) -> WorkflowRecord:
        """Persist a workflow and index it under the user."""
        record.updated_at = datetime.now()
        await self._set_with_ttl(
            self._key(
                self.key_config.workflow,
                user_id=user_id,
                workflow_id=record.id,
            ),
            record.model_dump_json(),
        )
        await self._client.sadd(
            self._key(self.key_config.workflow_index, user_id=user_id),
            record.id,
        )
        return record

    async def get_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> WorkflowRecord | None:
        """Fetch one workflow by id."""
        raw = await self._client.get(
            self._key(
                self.key_config.workflow,
                user_id=user_id,
                workflow_id=workflow_id,
            ),
        )
        return WorkflowRecord.model_validate_json(raw) if raw else None

    async def list_workflows(self, user_id: str) -> list[WorkflowRecord]:
        """Return every workflow in the user's index that still exists."""
        ids = await self._client.smembers(
            self._key(self.key_config.workflow_index, user_id=user_id),
        )
        records = []
        for workflow_id in ids:
            record = await self.get_workflow(user_id, workflow_id)
            if record is not None:
                records.append(record)
        return records

    async def delete_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> bool:
        """Delete a workflow."""
        await self._client.srem(
            self._key(self.key_config.workflow_index, user_id=user_id),
            workflow_id,
        )
        deleted = await self._client.delete(
            self._key(
                self.key_config.workflow,
                user_id=user_id,
                workflow_id=workflow_id,
            ),
        )
        return bool(deleted)
```

(Import `WorkflowRecord` alongside `SOPRecord`.)

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/workflow_storage_test.py -v
```

Expected: Redis test PASS; SQL test still FAIL (Task 4 implements it — temporarily mark it `@unittest.skip("SQL backend lands in Task 4")` if you want a green run now, then remove the skip in Task 4).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_base.py src/agentscope/app/storage/_redis_storage.py tests/workflow_storage_test.py
git commit -m "feat(workflow): redis-backed workflow storage"
```

---

### Task 4: Workflow storage — SQL backend + migration

**Files:**
- Modify: `src/agentscope/app/storage/_sql/_tables.py` (add `WorkflowRow` after `SOPRunRow`)
- Modify: `src/agentscope/app/storage/_sql/_mappers.py` (registry entry)
- Modify: `src/agentscope/app/storage/_sql/_storage.py` (methods next to the SOP block ~line 1055)
- Create: `src/agentscope/app/storage/_sql/_alembic/versions/0005_workflows.py`
- Test: un-skip `SqlWorkflowStorageTest` in `tests/workflow_storage_test.py`

- [ ] **Step 1: Remove the skip from the SQL test (written in Task 3)**

- [ ] **Step 2: Run it to verify it fails**

```bash
python -m pytest tests/workflow_storage_test.py::SqlWorkflowStorageTest -v
```

Expected: FAIL — `AttributeError` on the missing methods (once rows/mappers exist) or table-not-found.

- [ ] **Step 3: Implement**

In `_tables.py`:

```python
class WorkflowRow(_JsonRecordMixin):
    """One row per :class:`~agentscope.app.storage.WorkflowRecord`."""

    __tablename__ = "workflows"

    user_id: Mapped[str] = mapped_column(
        String(_ID_LEN),
        nullable=False,
        index=True,
    )

    _indexed_fields = ("user_id",)
```

In `_mappers.py`, add `WorkflowRecord: WorkflowRow` to the record→row registry exactly where `SOPRecord: SOPRow` appears (import both names following the file's existing import style).

In `_storage.py` (next to the SOP methods; import `WorkflowRow`, `WorkflowRecord`):

```python
    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------

    async def upsert_workflow(
        self,
        user_id: str,
        record: WorkflowRecord,
    ) -> WorkflowRecord:
        """Persist a workflow (create or overwrite)."""
        _ = user_id  # scoping is enforced by the caller
        return await self._write_row(WorkflowRow, record)

    async def get_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> WorkflowRecord | None:
        """Fetch one workflow; owner-scoped."""
        async with self._session() as sess:
            row = await sess.get(WorkflowRow, workflow_id)
        if row is None or row.user_id != user_id:
            return None
        return _to_record(row, WorkflowRecord)

    async def list_workflows(self, user_id: str) -> list[WorkflowRecord]:
        """Return the user's workflows."""
        from sqlalchemy import select

        async with self._session() as sess:
            rows = (
                (
                    await sess.execute(
                        select(WorkflowRow).where(
                            WorkflowRow.user_id == user_id,
                        ),
                    )
                )
                .scalars()
                .all()
            )
        return [_to_record(r, WorkflowRecord) for r in rows]

    async def delete_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> bool:
        """Delete a workflow."""
        async with self._session() as sess:
            row = await sess.get(WorkflowRow, workflow_id)
            if row is None or row.user_id != user_id:
                return False
            await sess.delete(row)
            await sess.commit()
        return True
```

Create `0005_workflows.py` (follow `0004_sops.py` exactly — single table):

```python
# -*- coding: utf-8 -*-
"""Workflows table.

Revision ID: 0005_workflows
Revises: 0004_sops
Create Date: 2026-10-08 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_workflows"
down_revision: Union[str, None] = "0004_sops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the ``workflows`` table."""
    op.create_table(
        "workflows",
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        for column in ("created_at", "updated_at", "user_id"):
            batch_op.create_index(
                batch_op.f(f"ix_workflows_{column}"),
                [column],
                unique=False,
            )


def downgrade() -> None:
    """Drop the ``workflows`` table."""
    op.drop_index(op.f("ix_workflows_user_id"), table_name="workflows")
    op.drop_index(op.f("ix_workflows_updated_at"), table_name="workflows")
    op.drop_index(op.f("ix_workflows_created_at"), table_name="workflows")
    op.drop_table("workflows")
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/workflow_storage_test.py -v
python -m pytest tests/storage_sql_test.py -v   # existing suite must stay green
```

Expected: all PASS (including the migration suite inside `storage_sql_test.py`, which runs `auto_migrate=True`).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_sql tests/workflow_storage_test.py
git commit -m "feat(workflow): sql-backed workflow storage with migration"
```

---

### Task 5: WorkflowService

**Files:**
- Create: `src/agentscope/app/_service/_workflow.py`
- Modify: `src/agentscope/app/_lifespan.py` (~line 200, after `workspace_service`), `src/agentscope/app/deps.py`
- Modify: `src/agentscope/app/_service/__init__.py` (export)

- [ ] **Step 1: Implement the service**

Create `src/agentscope/app/_service/_workflow.py`:

```python
# -*- coding: utf-8 -*-
"""Workflow service: CRUD plus the checks that need other records."""
from fastapi import HTTPException, status

from ..storage import (
    AgentNodeConfig,
    LoopNodeConfig,
    StorageBase,
    WorkflowData,
    WorkflowGraph,
    WorkflowRecord,
    validate_workflow_graph,
)


class WorkflowService:
    """Create, read, update and delete workflows, validating graphs."""

    def __init__(self, storage: StorageBase) -> None:
        self._storage = storage

    async def create_workflow(
        self,
        user_id: str,
        data: WorkflowData,
    ) -> WorkflowRecord:
        """Validate and persist a new workflow."""
        problems = await self._validate(user_id, data.graph)
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"problems": problems},
            )
        record = WorkflowRecord(user_id=user_id, data=data)
        return await self._storage.upsert_workflow(user_id, record)

    async def get_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> WorkflowRecord:
        """Fetch one workflow or answer 404."""
        record = await self._storage.get_workflow(user_id, workflow_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id!r} not found.",
            )
        return record

    async def list_workflows(self, user_id: str) -> list[WorkflowRecord]:
        """Every workflow the user owns."""
        return await self._storage.list_workflows(user_id)

    async def update_workflow(
        self,
        user_id: str,
        workflow_id: str,
        data: WorkflowData,
    ) -> WorkflowRecord:
        """Validate and overwrite a workflow, bumping its version."""
        record = await self.get_workflow(user_id, workflow_id)
        problems = await self._validate(user_id, data.graph)
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"problems": problems},
            )
        data.version = record.data.version + 1
        record.data = data
        return await self._storage.upsert_workflow(user_id, record)

    async def delete_workflow(self, user_id: str, workflow_id: str) -> None:
        """Delete a workflow or answer 404."""
        record = await self.get_workflow(user_id, workflow_id)
        await self._storage.delete_workflow(user_id, record.id)

    async def _validate(
        self,
        user_id: str,
        graph: WorkflowGraph,
    ) -> list[str]:
        """Structural checks plus agent existence."""
        problems = validate_workflow_graph(graph)
        agent_ids = {
            node.config.agent_id
            for node in graph.nodes
            if isinstance(node.config, AgentNodeConfig)
        }
        agent_ids.update(
            node.config.body_agent_id
            for node in graph.nodes
            if isinstance(node.config, LoopNodeConfig)
            and node.config.body_agent_id
        )
        for agent_id in sorted(agent_ids):
            if await self._storage.get_agent(user_id, agent_id) is None:
                problems.append(
                    f"Node references unknown agent {agent_id!r}",
                )
        return problems
```

In `_lifespan.py`, right after `app.state.workspace_service = ...`:

```python
        app.state.workflow_service = WorkflowService(storage=storage)
```

(import `WorkflowService` from `._service` alongside the other service imports).

In `deps.py` (next to `get_sop_service`):

```python
async def get_workflow_service(request: Request) -> WorkflowService:
    """Return the application-wide workflow service.

    Args:
        request (`Request`): The incoming FastAPI request.

    Returns:
        `WorkflowService`: The workflow service in ``app.state``.
    """
    return request.app.state.workflow_service
```

Export `WorkflowService` from `_service/__init__.py`.

- [ ] **Step 2: Verify the app still boots**

```bash
python -m pytest tests/app_sop_router_test.py -v
```

Expected: PASS (boots the full app via `create_app`).

- [ ] **Step 3: Commit**

```bash
git add src/agentscope/app/_service/_workflow.py src/agentscope/app/_service/__init__.py src/agentscope/app/_lifespan.py src/agentscope/app/deps.py
git commit -m "feat(workflow): workflow service with cross-record validation"
```

---

### Task 6: Workflow REST router

**Files:**
- Create: `src/agentscope/app/_router/_schema/_workflow.py`
- Create: `src/agentscope/app/_router/_workflow.py`
- Modify: `src/agentscope/app/_router/__init__.py`, `src/agentscope/app/_app.py` (import list ~line 12)
- Test: `tests/app_workflow_router_test.py`

- [ ] **Step 1: Write the failing test**

Create `tests/app_workflow_router_test.py`:

```python
# -*- coding: utf-8 -*-
"""Workflow CRUD endpoints over a real app and fake backends."""
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

HEADERS = {"X-User-ID": "alice"}


def _fake_backends() -> tuple:
    """Build a fakeredis-backed storage and message bus."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _Storage(RedisStorage):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    class _Bus(RedisMessageBus):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    return _Storage(), _Bus()


def _graph(agent_id: str) -> dict:
    """A two-node chain over the given agent."""
    return {
        "nodes": [
            {"id": "a", "config": {"type": "agent", "agent_id": agent_id}},
            {"id": "b", "config": {"type": "agent", "agent_id": agent_id}},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "b"}],
    }


class WorkflowRouterTest(IsolatedAsyncioTestCase):
    """CRUD, version bumps and validation over HTTP."""

    def setUp(self) -> None:
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        self._client = self.enterContext(
            TestClient(
                create_app(
                    storage=storage,
                    message_bus=bus,
                    workspace_manager=LocalWorkspaceManager(workdir),
                    enable_index_worker=False,
                ),
            ),
        )
        self._agent_id = self._client.post(
            "/agent/",
            json={"name": "worker", "system_prompt": "hi"},
            headers=HEADERS,
        ).json()["agent_id"]

    def _data(self) -> dict:
        return {
            "name": "pipeline",
            "description": "two steps",
            "graph": _graph(self._agent_id),
        }

    def test_workflow_round_trips_through_the_api(self) -> None:
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        workflow_id = created.json()["workflow_id"]

        fetched = self._client.get(
            f"/workflow/{workflow_id}",
            headers=HEADERS,
        )
        self.assertEqual(fetched.json()["data"]["name"], "pipeline")
        self.assertEqual(fetched.json()["data"]["version"], 1)

        listed = self._client.get("/workflow/", headers=HEADERS).json()
        self.assertEqual(listed["total"], 1)

        renamed = dict(self._data(), name="pipeline v2")
        patched = self._client.patch(
            f"/workflow/{workflow_id}",
            json={"data": renamed},
            headers=HEADERS,
        )
        self.assertEqual(patched.json()["data"]["name"], "pipeline v2")
        self.assertEqual(patched.json()["data"]["version"], 2)

        self.assertEqual(
            self._client.delete(
                f"/workflow/{workflow_id}",
                headers=HEADERS,
            ).status_code,
            204,
        )
        self.assertEqual(
            self._client.get(
                f"/workflow/{workflow_id}",
                headers=HEADERS,
            ).status_code,
            404,
        )

    def test_a_cyclic_graph_is_rejected_with_422(self) -> None:
        data = dict(
            self._data(),
            graph={
                "nodes": _graph(self._agent_id)["nodes"],
                "edges": [
                    {"id": "e1", "source": "a", "target": "b"},
                    {"id": "e2", "source": "b", "target": "a"},
                ],
            },
        )
        created = self._client.post(
            "/workflow/",
            json={"data": data},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)
        self.assertTrue(
            any(
                "Cycle detected" in p
                for p in created.json()["detail"]["problems"]
            ),
        )

    def test_unknown_agent_reference_is_rejected(self) -> None:
        data = dict(self._data(), graph=_graph("ghost"))
        created = self._client.post(
            "/workflow/",
            json={"data": data},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)
        self.assertTrue(
            any(
                "unknown agent" in p
                for p in created.json()["detail"]["problems"]
            ),
        )

    def test_workflows_are_scoped_per_user(self) -> None:
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        ).json()
        other = {"X-User-ID": "bob"}
        self.assertEqual(
            self._client.get(
                f"/workflow/{created['workflow_id']}",
                headers=other,
            ).status_code,
            404,
        )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/app_workflow_router_test.py -v
```

Expected: FAIL — 404 on `/workflow/` (router not registered).

- [ ] **Step 3: Implement schemas and router**

Create `src/agentscope/app/_router/_schema/_workflow.py`:

```python
# -*- coding: utf-8 -*-
"""Request/response schemas for the workflow endpoints."""
from pydantic import BaseModel, Field

from ...storage import (
    WorkflowData,
    WorkflowRecord,
)


class CreateWorkflowRequest(BaseModel):
    """Request body for creating a workflow."""

    data: WorkflowData = Field(description="The workflow's content.")


class CreateWorkflowResponse(BaseModel):
    """Response body after creating a workflow."""

    workflow_id: str = Field(description="Server-assigned workflow id.")


class WorkflowDetailResponse(BaseModel):
    """One workflow's content."""

    data: WorkflowData = Field(description="The workflow's content.")


class UpdateWorkflowRequest(BaseModel):
    """Request body for replacing a workflow's content."""

    data: WorkflowData = Field(description="The new content.")


class ListWorkflowsResponse(BaseModel):
    """Every workflow the caller owns."""

    workflows: list[WorkflowRecord] = Field(
        description="The caller's workflows.",
    )

    total: int = Field(description="How many there are.")
```

Create `src/agentscope/app/_router/_workflow.py`:

```python
# -*- coding: utf-8 -*-
"""Workflow router — CRUD endpoints for workflow definitions."""
from fastapi import APIRouter, Depends, status

from .._service import WorkflowService
from ..deps import get_current_user_id, get_workflow_service
from ._schema import (
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    ListWorkflowsResponse,
    UpdateWorkflowRequest,
    WorkflowDetailResponse,
)

workflow_router = APIRouter(
    prefix="/workflow",
    tags=["workflow"],
    responses={404: {"description": "Not found"}},
)


@workflow_router.post(
    "/",
    response_model=CreateWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow",
)
async def create_workflow(
    body: CreateWorkflowRequest,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> CreateWorkflowResponse:
    """Validate the graph and persist a new workflow."""
    record = await service.create_workflow(user_id, body.data)
    return CreateWorkflowResponse(workflow_id=record.id)


@workflow_router.get(
    "/",
    response_model=ListWorkflowsResponse,
    summary="List the caller's workflows",
)
async def list_workflows(
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> ListWorkflowsResponse:
    """Every workflow the caller owns."""
    workflows = await service.list_workflows(user_id)
    return ListWorkflowsResponse(workflows=workflows, total=len(workflows))


@workflow_router.get(
    "/{workflow_id}",
    response_model=WorkflowDetailResponse,
    summary="Fetch one workflow",
)
async def get_workflow(
    workflow_id: str,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowDetailResponse:
    """One workflow's content, or 404."""
    record = await service.get_workflow(user_id, workflow_id)
    return WorkflowDetailResponse(data=record.data)


@workflow_router.patch(
    "/{workflow_id}",
    response_model=WorkflowDetailResponse,
    summary="Update a workflow",
)
async def update_workflow(
    workflow_id: str,
    body: UpdateWorkflowRequest,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowDetailResponse:
    """Replace the content and bump the version."""
    record = await service.update_workflow(user_id, workflow_id, body.data)
    return WorkflowDetailResponse(data=record.data)


@workflow_router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workflow",
)
async def delete_workflow(
    workflow_id: str,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> None:
    """Delete a workflow, or 404."""
    await service.delete_workflow(user_id, workflow_id)
```

Wire-up:
1. `_router/_schema/__init__.py` — export the five new schema names (mirror how `_agent` schemas are exported).
2. `_router/__init__.py` — `from ._workflow import workflow_router`, add `"workflow_router"` to `__all__`.
3. `_app.py` — add `workflow_router` to the `from ._router import (...)` list (~line 12) and to the include loop's tuple (~line 395).

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/app_workflow_router_test.py tests/workflow_storage_test.py tests/agent_model_config_test.py tests/workflow_graph_test.py -v
```

Expected: all PASS.

- [ ] **Step 5: Run the neighbouring suites to catch regressions**

```bash
python -m pytest tests/app_sop_router_test.py tests/storage_redis_test.py tests/storage_sql_test.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/app tests/app_workflow_router_test.py
git commit -m "feat(workflow): workflow CRUD REST endpoints"
```

---

## Follow-up plans (not in scope here)

- **Plan 2 — Engine:** `WorkflowRunRecord` + node states, frontier scheduler, the four node handlers, run/approve/cancel/retry endpoints, SSE events, crash resume.
- **Plan 3 — Triggers:** scheduler → run, `run_workflow` chat tool, `POST /credential/test`.
- **Plan 4 — Frontend:** React Flow builder, run view, approvals inbox, agent template form fields.
