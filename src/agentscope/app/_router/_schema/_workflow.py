# -*- coding: utf-8 -*-
"""Request / response schemas for the workflow router."""
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...storage import (
    ChatModelConfig,
    WorkflowData,
    WorkflowRecord,
    WorkflowRunData,
    WorkflowRunRecord,
)


class CreateWorkflowRequest(BaseModel):
    """Request body for creating a workflow."""

    data: WorkflowData = Field(description="The workflow's content.")


class CreateWorkflowResponse(BaseModel):
    """Response body after creating a workflow."""

    workflow_id: str = Field(description="Server-assigned workflow id.")


class UpdateWorkflowRequest(BaseModel):
    """Request body for replacing a workflow's content."""

    data: WorkflowData = Field(description="The new content.")


class WorkflowDetailResponse(BaseModel):
    """One workflow's content."""

    data: WorkflowData = Field(description="The workflow's content.")


class ListWorkflowsResponse(BaseModel):
    """Response body listing the caller's workflows."""

    workflows: list[WorkflowRecord] = Field(
        description="The caller's workflows.",
    )

    total: int = Field(description="How many there are.")


class StartRunRequest(BaseModel):
    """Request body for starting a workflow run."""

    input: dict[str, Any] = Field(
        default_factory=dict,
        description="What the run's nodes template against.",
    )

    chat_model_config: ChatModelConfig | None = Field(
        default=None,
        description="Run-level default model for modelless agent nodes.",
    )


class StartRunResponse(BaseModel):
    """Response body after starting a run."""

    run_id: str = Field(description="Server-assigned run id.")


class ListRunsResponse(BaseModel):
    """The caller's workflow runs.

    Reflects whatever ``workflow_id`` / ``status`` filters the request
    carried; ``total`` counts the runs actually returned.
    """

    runs: list[WorkflowRunRecord] = Field(
        description="The runs, newest first.",
    )

    total: int = Field(description="How many there are.")


class RunDetailResponse(BaseModel):
    """One run's persisted state."""

    data: WorkflowRunData = Field(description="The run's content.")


class ApproveRequest(BaseModel):
    """Request body for filing an approval verdict."""

    node_id: str = Field(description="The approval node being answered.")

    verdict: Literal["approve", "reject"] = Field(
        description="approve follows the approve edge; reject the other.",
    )

    note: str = Field(
        default="",
        description="Optional note recorded with the verdict.",
    )
