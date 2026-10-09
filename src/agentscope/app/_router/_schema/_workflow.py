# -*- coding: utf-8 -*-
"""Request / response schemas for the workflow router."""
from pydantic import BaseModel, Field

from ...storage import WorkflowData, WorkflowRecord


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
