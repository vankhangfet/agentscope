# -*- coding: utf-8 -*-
"""Workflow router — CRUD for definitions, lifecycle for runs."""
from fastapi import APIRouter, Depends, status

from ..deps import (
    get_current_user_id,
    get_workflow_engine,
    get_workflow_service,
)
from ._schema import (
    ApproveRequest,
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    ListRunsResponse,
    ListWorkflowsResponse,
    RunDetailResponse,
    StartRunRequest,
    StartRunResponse,
    UpdateWorkflowRequest,
    WorkflowDetailResponse,
)
from .._service import WorkflowEngine, WorkflowService

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


# Run routes precede ``/{workflow_id}`` so "runs" is never captured
# as a workflow id by FastAPI's order-dependent path matching.
@workflow_router.post(
    "/{workflow_id}/run",
    response_model=StartRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a run of a workflow",
)
async def start_run(
    workflow_id: str,
    body: StartRunRequest,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> StartRunResponse:
    """Snapshot the workflow into a new run and dispatch it.

    Args:
        workflow_id (`str`):
            The workflow to run.
        body (`StartRunRequest`):
            The run's input and optional run-level default model.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.
        engine (`WorkflowEngine`):
            Injected run engine.

    Returns:
        `StartRunResponse`:
            The server-assigned run id.

    Raises:
        `HTTPException`:
            404 when the workflow is missing; 422 listing every
            problem (unknown agents, unresolvable models).
    """
    record = await service.start_run(
        user_id,
        workflow_id,
        body.input,
        chat_model_config=body.chat_model_config,
    )
    await engine.dispatch(user_id, record.id)
    return StartRunResponse(run_id=record.id)


@workflow_router.get(
    "/runs",
    response_model=ListRunsResponse,
    summary="List the caller's runs",
)
async def list_runs(
    workflow_id: str | None = None,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> ListRunsResponse:
    """Return the caller's runs, newest first.

    Args:
        workflow_id (`str | None`):
            Only this workflow's runs when given.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Returns:
        `ListRunsResponse`:
            The caller's runs, newest first.
    """
    runs = await service.list_runs(user_id, workflow_id)
    return ListRunsResponse(runs=runs, total=len(runs))


@workflow_router.get(
    "/runs/{run_id}",
    response_model=RunDetailResponse,
    summary="Read one run",
)
async def get_run(
    run_id: str,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> RunDetailResponse:
    """Return one run's persisted state.

    Args:
        run_id (`str`):
            The run to read.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Returns:
        `RunDetailResponse`:
            The run's content.

    Raises:
        `HTTPException`:
            404 when the caller owns no such run.
    """
    record = await service.get_run(user_id, run_id)
    return RunDetailResponse(data=record.data)


@workflow_router.post(
    "/runs/{run_id}/approve",
    response_model=RunDetailResponse,
    summary="Answer an approval node",
)
async def approve_run(
    run_id: str,
    body: ApproveRequest,
    user_id: str = Depends(get_current_user_id),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> RunDetailResponse:
    """Record a human verdict on a parked run and resume it.

    Args:
        run_id (`str`):
            The parked run.
        body (`ApproveRequest`):
            The node being answered and the verdict.
        user_id (`str`):
            Injected authenticated user id.
        engine (`WorkflowEngine`):
            Injected run engine.

    Returns:
        `RunDetailResponse`:
            The run's content with the verdict applied.

    Raises:
        `HTTPException`:
            404 when the run is missing; 409 when the node is not
            awaiting approval or the run already ended.
    """
    record = await engine.approve(
        user_id,
        run_id,
        body.node_id,
        body.verdict,
        body.note,
    )
    return RunDetailResponse(data=record.data)


@workflow_router.post(
    "/runs/{run_id}/cancel",
    response_model=RunDetailResponse,
    summary="Cancel a run",
)
async def cancel_run(
    run_id: str,
    user_id: str = Depends(get_current_user_id),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> RunDetailResponse:
    """Cancel a run: every non-terminal node ends CANCELLED.

    Args:
        run_id (`str`):
            The run to cancel.
        user_id (`str`):
            Injected authenticated user id.
        engine (`WorkflowEngine`):
            Injected run engine.

    Returns:
        `RunDetailResponse`:
            The cancelled run's content.

    Raises:
        `HTTPException`:
            404 when the run is missing.
    """
    record = await engine.cancel(user_id, run_id)
    return RunDetailResponse(data=record.data)


@workflow_router.post(
    "/runs/{run_id}/retry",
    response_model=RunDetailResponse,
    summary="Retry a failed run",
)
async def retry_run(
    run_id: str,
    user_id: str = Depends(get_current_user_id),
    engine: WorkflowEngine = Depends(get_workflow_engine),
) -> RunDetailResponse:
    """Reset a failed run's failed and cancelled nodes and re-dispatch.

    Args:
        run_id (`str`):
            The failed run.
        user_id (`str`):
            Injected authenticated user id.
        engine (`WorkflowEngine`):
            Injected run engine.

    Returns:
        `RunDetailResponse`:
            The run's content, pending again.

    Raises:
        `HTTPException`:
            404 when the run is missing; 409 when it did not fail.
    """
    record = await engine.retry(user_id, run_id)
    return RunDetailResponse(data=record.data)


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
