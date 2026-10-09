# -*- coding: utf-8 -*-
"""Run record models: the persisted state one workflow run walks
through. Pure pydantic, no I/O."""
from unittest import TestCase

from agentscope.app.storage import (
    AgentNodeConfig,
    NodeRunState,
    RunStatus,
    WorkflowGraph,
    WorkflowNode,
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
        self.assertEqual(state.status, RunStatus.PENDING)
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
        self.assertIsNone(data.chat_model_config)
        self.assertIsNone(data.error)
        self.assertIsNone(data.finished_at)

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
