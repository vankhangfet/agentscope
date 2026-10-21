# -*- coding: utf-8 -*-
"""Workflow router — CRUD for definitions, lifecycle for runs."""
import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from ..deps import (
    get_current_user_id,
    get_message_bus,
    get_workflow_engine,
    get_workflow_service,
)
from ..message_bus import MessageBus
from ..storage import RunStatus
from ._schema import (
    ApproveRequest,
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    ListRunsResponse,
    ListWorkflowTemplatesResponse,
    ListWorkflowsResponse,
    RunDetailResponse,
    StartRunRequest,
    StartRunResponse,
    UpdateWorkflowRequest,
    WorkflowDetailResponse,
    WorkflowTemplateView,
)
from .._service import (
    TEMPLATES,
    WorkflowEngine,
    WorkflowService,
    template_view,
)

workflow_router = APIRouter(
    prefix="/workflow",
    tags=["workflow"],
    responses={404: {"description": "Not found"}},
)


def _parse_run_status(value: str | None) -> RunStatus | None:
    """Translate the ``?status=`` query string into a RunStatus.

    Args:
        value (`str | None`):
            The raw query parameter, or ``None`` when absent.

    Returns:
        `RunStatus | None`:
            The matching status, or ``None`` to leave it unfiltered.

    Raises:
        `HTTPException`:
            422 listing the allowed values when *value* is not one.
    """
    if value is None:
        return None
    try:
        return RunStatus(value)
    except ValueError:
        allowed = ", ".join(member.value for member in RunStatus)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown status {value!r}; allowed values: {allowed}.",
        ) from None


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


# Declared before the ``/{workflow_id}`` routes, like the MCP router's
# ``/presets``: a literal path segment must not risk being captured as
# someone's record id.
@workflow_router.get(
    "/templates",
    response_model=ListWorkflowTemplatesResponse,
    summary="List the built-in workflow templates",
)
async def list_workflow_templates() -> ListWorkflowTemplatesResponse:
    """List the template catalog the gallery installs from.

    Static data with nothing per-user in it — the agent and channel
    references are empty slots the install dialog maps before the
    strict create — so it takes no auth and no storage, exactly like
    the MCP preset catalog.

    Returns:
        `ListWorkflowTemplatesResponse`: The catalog entries.
    """
    return ListWorkflowTemplatesResponse(
        templates=[
            WorkflowTemplateView(**template_view(t)) for t in TEMPLATES
        ],
        total=len(TEMPLATES),
    )


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
    status: str | None = Query(
        default=None,
        description="Only runs in this status (a RunStatus value).",
    ),
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
) -> ListRunsResponse:
    """Return the caller's runs, newest first.

    Args:
        workflow_id (`str | None`):
            Only this workflow's runs when given.
        status (`str | None`):
            Only runs in this status when given; one of the
            :class:`RunStatus` values.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service.

    Returns:
        `ListRunsResponse`:
            The caller's runs, newest first.

    Raises:
        `HTTPException`:
            422 when ``status`` is not a :class:`RunStatus` value.
    """
    run_status = _parse_run_status(status)
    runs = await service.list_runs(user_id, workflow_id, run_status)
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


# ----------------------------------------------------------------------
# Stream: live SSE connection for run events
# ----------------------------------------------------------------------

_HEARTBEAT_INTERVAL_SECS = 30
# Interval between SSE heartbeat comment frames (``:\n\n``).

_SUBSCRIBE_TIMEOUT_SECS = 5.0
# How long the stream waits for the bus subscription to go live.

_RUN_TERMINAL_STATUSES = frozenset(
    (
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    ),
)
# Status values that end a stream: once the run itself reports one of
# these, no further transition can occur (a retry is a fresh dispatch
# the client observes through a new connection).


@workflow_router.get(
    "/runs/{run_id}/stream",
    summary="Subscribe to a run's event stream (SSE)",
    response_description=(
        "Server-Sent Events stream of run state and node/run events"
    ),
)
async def stream_run_events(
    run_id: str,
    user_id: str = Depends(get_current_user_id),
    service: WorkflowService = Depends(get_workflow_service),
    message_bus: MessageBus = Depends(get_message_bus),
) -> StreamingResponse:
    """Subscribe to a run's live event stream.

    Returns a ``text/event-stream`` that opens with one ``state``
    frame — the full run record, in the same shape
    ``GET /workflow/runs/{run_id}`` returns — then forwards every
    transition the engine publishes for the run: each bus payload
    becomes an ``event: node`` or ``event: run`` frame whose data is
    the payload as-is (``{"type", "run_id", "node_id", "status",
    "error"}``).

    The stream ends when the run does: a run already terminal at
    connect emits its state and closes, and the first ``run`` frame
    carrying a terminal status closes an open one. Otherwise the
    connection stays open until the client disconnects. A heartbeat
    comment frame (``:\\n\\n``) is sent every 30 seconds to keep the
    connection alive through reverse proxies.

    Args:
        run_id (`str`):
            The run to subscribe to.
        user_id (`str`):
            Injected authenticated user id.
        service (`WorkflowService`):
            Injected workflow service; owner-scoped existence check
            and the state snapshot.
        message_bus (`MessageBus`):
            Injected message bus (live event subscription).

    Returns:
        `StreamingResponse`:
            SSE stream of ``state`` / ``node`` / ``run`` frames plus
            periodic heartbeats.

    Raises:
        `HTTPException`: 404 when the caller owns no such run.
    """
    # Owner-scoped existence check: raises the 404 before the stream
    # opens, so an unknown run answers as a normal error response.
    await service.get_run(user_id, run_id)

    async def _sse_generator() -> AsyncGenerator[str, None]:
        channel = f"workflow_run:{run_id}"
        ready = asyncio.Event()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def _feeder() -> None:
            """Read from the bus subscription and forward to the
            queue.

            Pushes ``None`` as a sentinel when the subscription ends
            (which in practice only happens if the bus shuts down).
            """
            try:
                async for payload in message_bus.subscribe(
                    channel,
                    on_ready=ready.set,
                ):
                    await queue.put(payload)
            except asyncio.CancelledError:
                pass
            finally:
                await queue.put(None)

        feeder_task = asyncio.create_task(
            _feeder(),
            name=f"run-sse-feeder:{run_id}",
        )

        try:
            # Subscribe first, then snapshot: a transition persisted in
            # between is already folded into the snapshot, and its
            # publish arrives over the subscription in order — applied
            # after the snapshot it converges to the same state.
            await asyncio.wait_for(
                ready.wait(),
                timeout=_SUBSCRIBE_TIMEOUT_SECS,
            )
            record = await service.get_run(user_id, run_id)
            state = RunDetailResponse(data=record.data).model_dump_json()
            yield f"event: state\ndata: {state}\n\n"
            if record.data.status.value in _RUN_TERMINAL_STATUSES:
                return
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(),
                        timeout=_HEARTBEAT_INTERVAL_SECS,
                    )
                except asyncio.TimeoutError:
                    yield ":\n\n"
                    continue
                if item is None:
                    break
                event = item.get("type")
                if event is None:
                    continue
                data = json.dumps(item, ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"
                if (
                    event == "run"
                    and item.get("status") in _RUN_TERMINAL_STATUSES
                ):
                    return
        finally:
            feeder_task.cancel()
            try:
                await feeder_task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
