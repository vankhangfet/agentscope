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


class TemplateSlotView(BaseModel):
    """One reference an install must map to a real record."""

    node_id: str = Field(
        description="The node whose config carries the empty reference.",
    )

    label: str = Field(
        description="What the mapped record is for — the agent's job, "
        "or the channel's role in the flow.",
    )

    hint: str = Field(
        default="",
        description="Extra guidance shown under the label.",
    )


class WorkflowTemplateView(BaseModel):
    """One catalog entry, as listed by ``GET /workflow/templates``."""

    id: str = Field(description="The template id.")

    name: str = Field(description="The name the created workflow takes.")

    description: str = Field(description="What the template does.")

    category: str = Field(description="The gallery group it shows under.")

    graph: dict = Field(
        description=(
            "The full graph with its slots empty: the agent nodes' "
            "``agent_id`` and the notify nodes' ``channel_id`` are "
            "``\"\"`` and must be mapped before POSTing the graph to "
            "``/workflow/`` — the save is strict about empty ones."
        ),
    )

    agent_slots: list[TemplateSlotView] = Field(
        description="Every agent node, with what it is for.",
    )

    channel_slots: list[TemplateSlotView] = Field(
        description="Every notify node, with what it is for.",
    )


class ListWorkflowTemplatesResponse(BaseModel):
    """The body of the template catalog listing."""

    templates: list[WorkflowTemplateView] = Field(
        description="The catalog entries.",
    )

    total: int = Field(description="How many entries the catalog holds.")
