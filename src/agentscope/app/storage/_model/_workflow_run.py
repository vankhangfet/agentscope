# -*- coding: utf-8 -*-
"""The workflow run models: a snapshot of the graph plus the state of
each node as the run walks it.

The graph is copied into the run at start time — later edits to the
workflow never affect an in-flight run.
"""
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    # The only storage models the engine mutates in place after
    # construction; revalidating assignments makes a bad value fail
    # here rather than on the next resume after a persist.
    model_config = ConfigDict(validate_assignment=True)

    status: RunStatus = RunStatus.PENDING
    """The node's own lifecycle (PENDING..SUCCEEDED/FAILED/CANCELLED)."""

    attempts: int = Field(default=0, ge=0, description="Dispatches so far.")

    output: dict[str, Any] | None = None
    """What the node wrote to the blackboard, if it finished."""

    error: str | None = None
    """The failure text when status is FAILED."""


class WorkflowRunData(BaseModel):
    """A run's persisted content."""

    # See NodeRunState: mutated in place by the engine hot loop, so
    # assignments revalidate (build-once siblings skip this).
    model_config = ConfigDict(validate_assignment=True)

    workflow_id: str
    """The workflow this run executes (id at start time)."""

    workflow_version: int
    """The workflow version snapshot."""

    workflow_name: str
    """The workflow's display name snapshot."""

    graph: WorkflowGraph
    """The graph snapshot this run walks."""

    input: dict[str, Any] = Field(default_factory=dict)
    """What the caller handed the run."""

    status: RunStatus = RunStatus.PENDING
    """The run's lifecycle."""

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

    finished_at: datetime | None = None
    """Set once the run reaches a terminal state."""

    @field_validator("graph", mode="before")
    @classmethod
    def _snapshot_graph(cls, value: Any) -> Any:
        """Own a deep copy: later edits to the source workflow graph
        never leak into this run."""
        if isinstance(value, WorkflowGraph):
            return value.model_copy(deep=True)
        return value


class WorkflowRunRecord(_RecordBase):
    """A stored workflow run."""

    user_id: str
    """The owner's user id."""

    data: WorkflowRunData
    """The run's content."""
