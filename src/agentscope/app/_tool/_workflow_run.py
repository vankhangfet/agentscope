# -*- coding: utf-8 -*-
"""The workflow run tool: start a workflow from a chat turn."""
import asyncio
import time
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from ...message import TextBlock, ToolResultState
from ...permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ...tool import ToolBase, ToolChunk
from ..storage import (
    AgentNodeConfig,
    NodeRunState,
    RunStatus,
    StorageBase,
    WorkflowNode,
    WorkflowRecord,
    WorkflowRunRecord,
)

if TYPE_CHECKING:
    from .._service import WorkflowEngine, WorkflowService


_POLL_INTERVAL_SECS = 0.5
"""How long the tool sleeps between run polls."""

_OUTPUT_TEXT_LIMIT = 500
"""Characters of an agent node's text shown in the summary."""

_STOP_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.WAITING_HUMAN,
}
"""The statuses worth stopping the wait for: every terminal one plus a
run parked on an approval, which will not move without a human."""


class _WorkflowRunParams(BaseModel):
    """The params for the run workflow tool."""

    workflow_name_or_id: str = Field(
        description="Workflow id, or its unique name.",
    )

    input: dict[str, Any] = Field(
        default_factory=dict,
        description="Input the workflow's node templates render against.",
    )

    wait_seconds: int = Field(
        default=30,
        ge=0,
        le=300,
        description="How long to wait for the run before returning.",
    )


class WorkflowRun(ToolBase):
    """The workflow run tool.

    Starts a workflow for the calling user — resolving the target by
    exact id first, then by unique name — dispatches it through the
    engine, and waits up to ``wait_seconds`` for the outcome. The
    result is a run summary (status, one line per node, agent output
    text), the run id when the wait runs out, or an error chunk for
    everything the workflow subsystem refuses or throws. The tool
    never raises: a chat turn survives any of it.
    """

    name: str = "WorkflowRun"

    description: str = (
        "Start a workflow by name or id and optionally wait for it to "
        "finish. Returns the run's outcome summary, or the run id when "
        "it is still running."
    )

    input_schema: dict = _WorkflowRunParams.model_json_schema()

    is_concurrency_safe: bool = False
    is_read_only: bool = False
    is_state_injected: bool = False
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: str | None = None

    def __init__(
        self,
        user_id: str,
        storage: StorageBase,
        workflow_service: "WorkflowService",
        workflow_engine: "WorkflowEngine",
    ) -> None:
        """Bind the tool to one user's workflow wiring.

        Args:
            user_id (`str`): The user whose workflows this tool runs.
            storage (`StorageBase`): Application storage, used to poll
                the run while waiting.
            workflow_service (`WorkflowService`): Resolves the target
                and creates the run.
            workflow_engine (`WorkflowEngine`): Dispatches the run.
        """
        super().__init__()
        self._user_id = user_id
        self._storage = storage
        self._workflow_service = workflow_service
        self._workflow_engine = workflow_engine

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Check permission for the tool usage."""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed to be called.",
        )

    async def call(
        self,
        workflow_name_or_id: str,
        # pylint: disable-next=redefined-builtin
        input: dict[str, Any] | None = None,
        wait_seconds: int = 30,
    ) -> ToolChunk:
        """Start a workflow and report how it went.

        Args:
            workflow_name_or_id (`str`): Workflow id, or its unique
                name.
            input (`dict[str, Any]`, optional): The run's input.
            wait_seconds (`int`, optional): How long to wait for the
                run before returning its id.

        Returns:
            `ToolChunk`:
                The run's outcome summary, a still-running notice, or
                an error description. Never raises.
        """
        try:
            return await self._run(
                workflow_name_or_id,
                input or {},
                wait_seconds,
            )
        except HTTPException as exc:
            return _error_chunk(_render_http_detail(exc))
        except Exception as exc:  # pylint: disable=broad-except
            return _error_chunk(f"WorkflowRun failed: {exc}")

    async def _run(
        self,
        value: str,
        input: dict[str, Any],  # pylint: disable=redefined-builtin
        wait_seconds: int,
    ) -> ToolChunk:
        """Resolve, start, dispatch and wait — see :meth:`call`."""
        record = await self._resolve(value)
        run = await self._workflow_service.start_run(
            self._user_id,
            record.id,
            input=input,
            trigger="chat_tool",
        )
        await self._workflow_engine.dispatch(self._user_id, run.id)
        deadline = time.monotonic() + wait_seconds
        while True:
            current = await self._storage.get_workflow_run(
                self._user_id,
                run.id,
            )
            if current is None:
                return _error_chunk(
                    f"Run {run.id} disappeared while waiting for it.",
                )
            if current.data.status in _STOP_STATUSES:
                return _outcome_chunk(current)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_POLL_INTERVAL_SECS)
        return ToolChunk(
            content=[
                TextBlock(
                    text=(
                        f"Run {run.id} is still RUNNING — follow it on "
                        "the runs page."
                    ),
                ),
            ],
            state=ToolResultState.SUCCESS,
        )

    async def _resolve(self, value: str) -> WorkflowRecord:
        """The workflow the value names: by exact id, else by name.

        Raises:
            `HTTPException`: 404 when nothing matches, 409 when the
                name is ambiguous — both rendered as error chunks by
                :meth:`call`.
        """
        try:
            return await self._workflow_service.get_workflow(
                self._user_id,
                value,
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
        workflows = await self._workflow_service.list_workflows(
            self._user_id,
        )
        matches = [wf for wf in workflows if wf.data.name == value]
        if not matches:
            lowered = value.lower()
            matches = [
                wf for wf in workflows if wf.data.name.lower() == lowered
            ]
        if not matches:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No workflow found with id or name {value!r}.",
            )
        if len(matches) > 1:
            candidates = ", ".join(
                f"{wf.data.name} ({wf.id})" for wf in matches
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Multiple workflows are named {value!r}: "
                    f"{candidates}. Pass the id of the one you mean."
                ),
            )
        return matches[0]


def _render_http_detail(exc: HTTPException) -> str:
    """An HTTPException's detail as tool-readable text.

    A 422 from ``start_run`` carries a problems list, which gets a
    friendly header; anything else renders its detail as-is.
    """
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("problems"):
        problems = detail["problems"]
        lines = ["The workflow run could not be started:"]
        lines += [f"- {problem}" for problem in problems]
        return "\n".join(lines)
    return f"WorkflowRun failed: {detail}"


def _error_chunk(text: str) -> ToolChunk:
    """An error-state chunk carrying *text*."""
    return ToolChunk(
        content=[TextBlock(text=text)],
        state=ToolResultState.ERROR,
    )


def _outcome_chunk(run: WorkflowRunRecord) -> ToolChunk:
    """A stopped run as a chunk: parked runs point at the runs page,
    terminal ones get the full summary."""
    if run.data.status is RunStatus.WAITING_HUMAN:
        return ToolChunk(
            content=[
                TextBlock(
                    text=(
                        f"Run {run.id} is WAITING_HUMAN — it needs an "
                        "approval before it can continue; follow it on "
                        "the runs page."
                    ),
                ),
            ],
            state=ToolResultState.SUCCESS,
        )
    lines = [f"Run {run.id} {run.data.status.value}:"]
    for node in run.data.graph.nodes:
        state = run.data.node_states.get(node.id, NodeRunState())
        lines.append(f"  {node.id}={state.status.value}")
        text = _node_text(run, node, state.status)
        if text is not None:
            lines.append(f"    {text}")
    if run.data.error:
        lines.append(f"Error: {run.data.error}")
    return ToolChunk(
        content=[TextBlock(text="\n".join(lines))],
        state=(
            ToolResultState.ERROR
            if run.data.status is RunStatus.FAILED
            else ToolResultState.SUCCESS
        ),
    )


def _node_text(
    run: WorkflowRunRecord,
    node: WorkflowNode,
    status: RunStatus,
) -> str | None:
    """A succeeded agent node's text output, clipped — None for
    everything else (other node types, failed nodes, empty output)."""
    if not isinstance(node.config, AgentNodeConfig):
        return None
    if status is not RunStatus.SUCCEEDED:
        return None
    output = run.data.context.get(node.id)
    text = output.get("text") if isinstance(output, dict) else None
    if not isinstance(text, str) or not text:
        return None
    if len(text) > _OUTPUT_TEXT_LIMIT:
        return text[:_OUTPUT_TEXT_LIMIT] + " … (truncated)"
    return text
