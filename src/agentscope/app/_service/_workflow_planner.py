# -*- coding: utf-8 -*-
"""Frontier computation over a workflow graph and its node states."""
from ..storage import (
    NodeRunState,
    RunStatus,
    WorkflowEdge,
    WorkflowGraph,
)

# States that bar a node from being (re)selected by the planner.
_BUSY = {
    RunStatus.RUNNING,
    RunStatus.WAITING_HUMAN,
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}

_TERMINAL = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


def ready_nodes(
    graph: WorkflowGraph,
    node_states: dict[str, NodeRunState],
) -> set[str]:
    """Which nodes have every incoming edge satisfied.

    An edge is satisfied when its source SUCCEEDED and — when the edge
    carries a branch label — the source's output selected that branch
    (``output["branch"]``). Nodes in any non-PENDING state are never
    re-selected; a FAILED or CANCELLED source satisfies nothing.

    Args:
        graph (`WorkflowGraph`): The run's graph snapshot.
        node_states (`dict[str, NodeRunState]`): Node id → state.

    Returns:
        `set[str]`: Ids that may dispatch now.
    """

    def state(node_id: str) -> NodeRunState:
        return node_states.get(node_id, NodeRunState())

    incoming: dict[str, list[WorkflowEdge]] = {
        node.id: [] for node in graph.nodes
    }
    for edge in graph.edges:
        incoming[edge.target].append(edge)

    ready: set[str] = set()
    for node in graph.nodes:
        if state(node.id).status in _BUSY:
            continue
        satisfied = True
        for edge in incoming[node.id]:
            src = state(edge.source)
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
    """A run is finished when every node reached a terminal state.

    Args:
        graph (`WorkflowGraph`): The run's graph snapshot.
        node_states (`dict[str, NodeRunState]`): Node id → state.

    Returns:
        `bool`: True when no node can ever dispatch again.
    """
    return all(
        node_states.get(node.id, NodeRunState()).status in _TERMINAL
        for node in graph.nodes
    )
