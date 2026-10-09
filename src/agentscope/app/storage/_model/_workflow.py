# -*- coding: utf-8 -*-
"""The workflow storage models: a DAG of nodes a run walks through.

The graph mirrors React Flow's shape (nodes carry canvas ``position``,
edges carry ids) so the builder UI persists what it renders; the engine
reads only ``config`` and ignores presentation fields.
"""
from collections import Counter
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

    label: str = Field(
        min_length=1,
        description="Matches an outgoing edge's branch.",
    )

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

    id: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
        description=(
            "Unique node id within the workflow. Must start with an "
            "alphanumeric and continue with alphanumerics, dashes or "
            "underscores: colons are reserved for the engine's derived "
            "session keys, and a leading underscore for the blackboard's "
            "bookkeeping filter."
        ),
    )

    position: WorkflowPosition = Field(
        default_factory=WorkflowPosition,
        description="Canvas coordinates (React Flow).",
    )

    config: WorkflowNodeConfig


class WorkflowEdge(BaseModel):
    """A directed connection between two nodes."""

    id: str = Field(
        min_length=1,
        description="Unique edge id within the workflow.",
    )

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

    node_counts = Counter(node.id for node in graph.nodes)
    duplicates = {i for i, c in node_counts.items() if c > 1}
    if duplicates:
        problems.append(f"Duplicate node ids: {sorted(duplicates)}")
    known = set(node_counts)

    edge_counts = Counter(edge.id for edge in graph.edges)
    edge_dupes = {i for i, c in edge_counts.items() if c > 1}
    if edge_dupes:
        problems.append(f"Duplicate edge ids: {sorted(edge_dupes)}")

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
            labels = [b.label for b in node.config.branches]
            dupes = {
                label for label, c in Counter(labels).items() if c > 1
            }
            if dupes:
                problems.append(
                    f"Condition node {node.id} has duplicate branch "
                    f"labels: {sorted(dupes)}",
                )
            _check_branches(
                node.id,
                out,
                set(labels),
                problems,
                unlabeled_is_error=True,
            )
        elif isinstance(node.config, HumanApprovalNodeConfig):
            cfg = node.config
            allowed = {"approve", "reject"}
            if cfg.timeout_branch != "fail":
                allowed.add(cfg.timeout_branch)
            labelled = _check_branches(
                node.id,
                out,
                allowed,
                problems,
                unlabeled_is_error=True,
            )
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
    branch_counts = Counter(e.branch for e in out if e.branch is not None)
    for label, count in branch_counts.items():
        if count > 1:
            problems.append(
                f"Node {node_id} has {count} edges for branch {label!r}",
            )
    labelled = set(branch_counts)
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
