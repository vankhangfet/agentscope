# -*- coding: utf-8 -*-
"""Structural validation of workflow graphs: the checks that run before
anything is persisted. Pure functions, no I/O."""
from unittest import TestCase

from pydantic import ValidationError

from agentscope.app.storage import (
    AgentNodeConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    WorkflowData,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRecord,
    validate_workflow_graph,
)


def _graph(
    *nodes: WorkflowNode,
    edges: dict[str, WorkflowEdge] | None = None,
) -> WorkflowGraph:
    """Assemble a graph from nodes; edges keyed by their id."""
    return WorkflowGraph(
        nodes=list(nodes),
        edges=list((edges or {}).values()),
    )


def _agent(node_id: str) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        config=AgentNodeConfig(agent_id="agent-1"),
    )


def _e(
    edge_id: str,
    source: str,
    target: str,
    branch: str | None = None,
) -> WorkflowEdge:
    return WorkflowEdge(
        id=edge_id,
        source=source,
        target=target,
        branch=branch,
    )


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
            any(
                "Duplicate node ids" in p
                for p in validate_workflow_graph(graph)
            ),
        )

    def test_duplicate_edge_ids(self) -> None:
        graph = _graph(
            _agent("a"),
            _agent("b"),
            _agent("c"),
            edges={
                "first": _e("e1", "a", "b"),
                "second": _e("e1", "b", "c"),
            },
        )
        self.assertTrue(
            any(
                "Duplicate edge ids" in p
                for p in validate_workflow_graph(graph)
            ),
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

    def test_duplicate_branch_labels_in_one_condition(self) -> None:
        cond = WorkflowNode(
            id="c",
            config=ConditionNodeConfig(
                branches=[
                    ConditionBranch(label="yes", rule="input.a == true"),
                    ConditionBranch(label="yes", rule="input.b == true"),
                ],
            ),
        )
        graph = _graph(
            cond,
            _agent("x"),
            edges={"e1": _e("e1", "c", "x", branch="yes")},
        )
        problems = validate_workflow_graph(graph)
        self.assertTrue(
            any("duplicate branch labels" in p for p in problems),
        )

    def test_two_edges_for_one_branch_are_ambiguous(self) -> None:
        cond = WorkflowNode(
            id="c",
            config=ConditionNodeConfig(
                branches=[ConditionBranch(label="yes", rule="true")],
            ),
        )
        graph = _graph(
            cond,
            _agent("x"),
            _agent("y"),
            edges={
                "e1": _e("e1", "c", "x", branch="yes"),
                "e2": _e("e2", "c", "y", branch="yes"),
            },
        )
        problems = validate_workflow_graph(graph)
        self.assertTrue(
            any("2 edges for branch 'yes'" in p for p in problems),
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

    def test_approval_timeout_branch_with_matching_edge_passes(self) -> None:
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
                "e4": _e("e4", "h", "out", branch="escalate"),
            },
        )
        self.assertEqual(validate_workflow_graph(graph), [])

    def test_iterate_loop_requires_list_template(self) -> None:
        with self.assertRaises(ValueError):
            LoopNodeConfig(mode="iterate", list_template="")

    def test_retry_loop_requires_body_and_verifier(self) -> None:
        with self.assertRaises(ValueError):
            LoopNodeConfig(mode="retry_until", body_agent_id="a")
        with self.assertRaises(ValueError):
            LoopNodeConfig(mode="retry_until", verifier_prompt="good?")

    def test_empty_graph_is_rejected_by_the_schema(self) -> None:
        with self.assertRaises(ValidationError):
            WorkflowGraph(nodes=[], edges=[])

    def test_node_ids_keep_the_engine_namespace_clean(self) -> None:
        """Colons belong to derived session keys and a leading
        underscore to the blackboard's bookkeeping filter, so a node
        id may use neither; alphanumerics, dashes and underscores
        are fine."""
        with self.assertRaises(ValidationError):
            WorkflowNode(id="x:judge", config=_agent("ok").config)
        with self.assertRaises(ValidationError):
            WorkflowNode(id="_sessions", config=_agent("ok").config)
        self.assertEqual(
            WorkflowNode(id="a-b_2", config=_agent("ok").config).id,
            "a-b_2",
        )


class WorkflowGraphModelTest(TestCase):
    """The models themselves, beyond the validation rules."""

    def test_record_round_trips_through_json(self) -> None:
        record = WorkflowRecord(
            user_id="user-1",
            data=WorkflowData(
                name="One of each",
                description="A graph using every node config.",
                graph=WorkflowGraph(
                    nodes=[
                        WorkflowNode(
                            id="start",
                            config=AgentNodeConfig(
                                agent_id="agent-1",
                            ),
                        ),
                        WorkflowNode(
                            id="check",
                            config=ConditionNodeConfig(
                                branches=[
                                    ConditionBranch(
                                        label="yes",
                                        rule="input.ok == true",
                                    ),
                                ],
                            ),
                        ),
                        WorkflowNode(
                            id="fan",
                            config=LoopNodeConfig(
                                mode="iterate",
                                list_template="{{input.items}}",
                            ),
                        ),
                        WorkflowNode(
                            id="gate",
                            config=HumanApprovalNodeConfig(
                                timeout_branch="escalate",
                            ),
                        ),
                    ],
                    edges=[
                        WorkflowEdge(
                            id="e1",
                            source="start",
                            target="check",
                        ),
                        WorkflowEdge(
                            id="e2",
                            source="check",
                            target="fan",
                            branch="yes",
                        ),
                        WorkflowEdge(
                            id="e3",
                            source="fan",
                            target="gate",
                        ),
                    ],
                ),
            ),
        )
        revived = WorkflowRecord.model_validate_json(
            record.model_dump_json(),
        )
        self.assertEqual(revived, record)

    def test_deep_chain_does_not_recurse(self) -> None:
        chain = [_agent(f"n{i}") for i in range(10_000)]
        edges = [
            WorkflowEdge(id=f"e{i}", source=f"n{i}", target=f"n{i + 1}")
            for i in range(10_000 - 1)
        ]
        edges.append(
            WorkflowEdge(id="back", source="n9999", target="n0"),
        )
        problems = validate_workflow_graph(
            WorkflowGraph(nodes=chain, edges=edges),
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("Cycle detected", problems[0])
