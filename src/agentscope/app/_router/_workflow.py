# -*- coding: utf-8 -*-
"""Workflow router — CRUD endpoints for workflow definitions."""
from fastapi import APIRouter, Depends, status

from ..deps import get_current_user_id, get_workflow_service
from ._schema import (
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    ListWorkflowsResponse,
    UpdateWorkflowRequest,
    WorkflowDetailResponse,
)
from .._service import WorkflowService

workflow_router = APIRouter(
    prefix="/workflow",
    tags=["workflow"],
    responses={404: {"description": "Not found"}},
)


@workflow_router.post(
    "/",
    response_model=CreateWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow",
)
async def create_workflow(
    body: CreateWorkflowRequest,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> CreateWorkflowResponse:
    """Validate the graph and persist a new workflow.

    Args:
        body (`CreateWorkflowRequest`):
            The workflow to store.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Returns:
        `CreateWorkflowResponse`:
            The server-assigned identifier.

    Raises:
        `HTTPException`:
            422 when the graph fails validation.
    """
    record = await service.create_workflow(user_id, body.data)
    return CreateWorkflowResponse(workflow_id=record.id)


@workflow_router.get(
    "/",
    response_model=ListWorkflowsResponse,
    summary="List the caller's workflows",
)
async def list_workflows(
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> ListWorkflowsResponse:
    """Return every workflow the caller owns.

    Args:
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Returns:
        `ListWorkflowsResponse`:
            The caller's workflows, oldest first.
    """
    workflows = await service.list_workflows(user_id)
    return ListWorkflowsResponse(workflows=workflows, total=len(workflows))


@workflow_router.get(
    "/{workflow_id}",
    response_model=WorkflowDetailResponse,
    summary="Read one workflow",
)
async def get_workflow(
    workflow_id: str,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowDetailResponse:
    """Return one workflow's content.

    Args:
        workflow_id (`str`):
            The workflow to read.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Returns:
        `WorkflowDetailResponse`:
            The workflow's content.

    Raises:
        `HTTPException`:
            404 when the caller owns no such workflow.
    """
    record = await service.get_workflow(user_id, workflow_id)
    return WorkflowDetailResponse(data=record.data)


@workflow_router.patch(
    "/{workflow_id}",
    response_model=WorkflowDetailResponse,
    summary="Replace a workflow's contents",
)
async def update_workflow(
    workflow_id: str,
    body: UpdateWorkflowRequest,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> WorkflowDetailResponse:
    """Validate and overwrite a workflow, bumping its version.

    Args:
        workflow_id (`str`):
            The workflow to update.
        body (`UpdateWorkflowRequest`):
            The workflow as it should now read.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Returns:
        `WorkflowDetailResponse`:
            The stored content, version bumped.

    Raises:
        `HTTPException`:
            404 when the caller owns no such workflow; 422 when the
            graph fails validation.
    """
    record = await service.update_workflow(user_id, workflow_id, body.data)
    return WorkflowDetailResponse(data=record.data)


@workflow_router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workflow",
)
async def delete_workflow(
    workflow_id: str,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> None:
    """Delete one workflow.

    Args:
        workflow_id (`str`):
            The workflow to delete.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Raises:
        `HTTPException`:
            404 when the caller owns no such workflow.
    """
    await service.delete_workflow(user_id, workflow_id)
