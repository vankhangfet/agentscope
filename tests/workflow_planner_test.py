# -*- coding: utf-8 -*-
"""The frontier computation: which nodes may run right now."""
from unittest import TestCase

from agentscope.app._service._workflow_planner import (
    ready_nodes,
    run_is_finished,
)
from agentscope.app.storage import (
    AgentNodeConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    NodeRunState,
    RunStatus,
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
                ConditionBranch(label=label, rule="true")
                for label in labels
            ],
        ),
    )


def _graph(
    nodes: list[WorkflowNode],
    edges: list[tuple[str, str, str | None]],
) -> WorkflowGraph:
    """Assemble a graph; edges are (source, target, branch)."""
    return WorkflowGraph(
        nodes=nodes,
        edges=[
            WorkflowEdge(id=f"e{i}", source=s, target=t, branch=b)
            for i, (s, t, b) in enumerate(edges)
        ],
    )


class ReadyNodesTest(TestCase):
    """Who may dispatch, given node states."""

    def test_roots_are_ready(self) -> None:
        graph = _graph([_agent("a"), _agent("b")], [])
        self.assertEqual(ready_nodes(graph, {}), {"a", "b"})

    def test_successor_waits_for_source(self) -> None:
        graph = _graph([_agent("a"), _agent("b")], [("a", "b", None)])
        states = {"a": NodeRunState(status=RunStatus.RUNNING)}
        self.assertEqual(ready_nodes(graph, states), set())
        states = {"a": NodeRunState(status=RunStatus.SUCCEEDED)}
        self.assertEqual(ready_nodes(graph, states), {"b"})

    def test_only_the_taken_branch_unlocks(self) -> None:
        graph = _graph(
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
        graph = _graph(
            [_cond("c", "yes", "no"), _agent("y")],
            [("c", "y", "yes")],
        )
        states = {"c": NodeRunState(status=RunStatus.SUCCEEDED)}
        self.assertEqual(ready_nodes(graph, states), set())

    def test_failed_source_unlocks_nothing(self) -> None:
        graph = _graph([_agent("a"), _agent("b")], [("a", "b", None)])
        states = {"a": NodeRunState(status=RunStatus.FAILED)}
        self.assertEqual(ready_nodes(graph, states), set())

    def test_parked_node_is_not_redispatched(self) -> None:
        graph = _graph(
            [
                _agent("a"),
                WorkflowNode(id="h", config=HumanApprovalNodeConfig()),
                _agent("out"),
            ],
            [
                ("a", "h", None),
                ("h", "out", "approve"),
                ("h", "out", "reject"),
            ],
        )
        states = {
            "a": NodeRunState(status=RunStatus.SUCCEEDED),
            "h": NodeRunState(status=RunStatus.WAITING_HUMAN),
        }
        self.assertEqual(ready_nodes(graph, states), set())

    def test_diamond_merges(self) -> None:
        graph = _graph(
            [_agent("a"), _agent("b"), _agent("c"), _agent("d")],
            [
                ("a", "b", None),
                ("a", "c", None),
                ("b", "d", None),
                ("c", "d", None),
            ],
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
        graph = _graph([_agent("a")], [])
        states = {"a": NodeRunState(status=RunStatus.SUCCEEDED)}
        self.assertTrue(run_is_finished(graph, states))

    def test_any_failed_or_cancelled_is_finished(self) -> None:
        graph = _graph([_agent("a"), _agent("b")], [])
        states = {
            "a": NodeRunState(status=RunStatus.SUCCEEDED),
            "b": NodeRunState(status=RunStatus.FAILED),
        }
        self.assertTrue(run_is_finished(graph, states))
        states["b"] = NodeRunState(status=RunStatus.CANCELLED)
        self.assertTrue(run_is_finished(graph, states))

    def test_in_flight(self) -> None:
        graph = _graph([_agent("a")], [])
        self.assertFalse(run_is_finished(graph, {}))
        states = {"a": NodeRunState(status=RunStatus.WAITING_HUMAN)}
        self.assertFalse(run_is_finished(graph, states))
