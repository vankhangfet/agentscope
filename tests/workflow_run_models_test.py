# -*- coding: utf-8 -*-
"""Run record models: the persisted state one workflow run walks
through. Pure pydantic, no I/O."""
from datetime import datetime
from unittest import TestCase

from pydantic import ValidationError

from agentscope.app.storage import (
    AgentNodeConfig,
    NodeRunState,
    RunStatus,
    SessionConfig,
    SessionRecord,
    SOPOrigin,
    WorkflowGraph,
    WorkflowNode,
    WorkflowOrigin,
    WorkflowRunData,
    WorkflowRunRecord,
)
from agentscope.state import AgentState


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
        self.assertEqual(state.status, RunStatus.PENDING)
        self.assertEqual(state.attempts, 0)
        self.assertIsNone(state.output)
        self.assertIsNone(state.error)

    def test_run_data_holds_a_graph_snapshot(self) -> None:
        graph = _graph()
        data = WorkflowRunData(
            workflow_id="wf-1",
            workflow_version=3,
            workflow_name="pipeline",
            graph=graph,
            input={"topic": "q3"},
        )
        graph.nodes[0].id = "mutated"
        self.assertEqual(data.graph.nodes[0].id, "a")
        self.assertEqual(data.status, RunStatus.PENDING)
        self.assertEqual(data.trigger, "manual")
        self.assertEqual(data.context, {})
        self.assertEqual(data.node_states, {})
        self.assertIsNone(data.chat_model_config)
        self.assertIsNone(data.error)
        self.assertIsNone(data.finished_at)

    def test_assignment_is_validated(self) -> None:
        state = NodeRunState()
        with self.assertRaises(ValidationError):
            state.attempts = -1

        data = WorkflowRunData(
            workflow_id="wf-1",
            workflow_version=1,
            workflow_name="p",
            graph=_graph(),
        )
        with self.assertRaises(ValidationError):
            data.status = "BOGUS"
        data.status = "SUCCEEDED"
        self.assertEqual(data.status, RunStatus.SUCCEEDED)

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
                        status=RunStatus.WAITING_HUMAN,
                        attempts=1,
                        output={"text": "draft"},
                    ),
                },
                finished_at=datetime(2026, 10, 9, 12, 0),
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
        self.assertEqual(
            restored.data.finished_at,
            datetime(2026, 10, 9, 12, 0),
        )

    def test_workflow_origin_round_trips_in_a_session(self) -> None:
        """A workflow-opened session carries its origin through JSON."""
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
        self.assertEqual(restored.origin.workflow_run_id, "run-1")
        self.assertEqual(restored.origin.node_id, "research")

    def test_sop_origin_still_validates(self) -> None:
        """Extending the union leaves its older members parseable."""
        restored = SessionRecord.model_validate(
            {
                "id": "s1",
                "user_id": "u1",
                "agent_id": "ag1",
                "origin": {
                    "type": "sop",
                    "sop_run_id": "r",
                    "session_key": "k",
                },
                "config": {"workspace_id": "ws", "name": "n"},
            },
        )
        self.assertIsInstance(restored.origin, SOPOrigin)
        self.assertEqual(restored.origin.sop_run_id, "r")
        self.assertEqual(restored.origin.session_key, "k")
