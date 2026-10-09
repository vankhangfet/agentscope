# -*- coding: utf-8 -*-
"""Workflow service: CRUD plus the checks that need other records."""
from fastapi import HTTPException, status

from ..storage import (
    AgentNodeConfig,
    LoopNodeConfig,
    StorageBase,
    WorkflowData,
    WorkflowGraph,
    WorkflowRecord,
    validate_workflow_graph,
)


class WorkflowService:
    """Create, read, update and delete workflows, validating graphs.

    Records are always constructed or loaded with ``user_id`` set by
    this service, so storage-level owner scoping holds by construction.
    """

    def __init__(self, storage: StorageBase) -> None:
        """Initialize the service.

        Args:
            storage (`StorageBase`): Application storage.
        """
        self._storage = storage

    async def create_workflow(
        self,
        user_id: str,
        data: WorkflowData,
    ) -> WorkflowRecord:
        """Validate and persist a new workflow.

        Args:
            user_id (`str`): The owner's user id.
            data (`WorkflowData`): The workflow's content.

        Returns:
            `WorkflowRecord`: The persisted record.

        Raises:
            `HTTPException`: 422 when the graph fails validation.
        """
        problems = await self._validate(user_id, data.graph)
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"problems": problems},
            )
        record = WorkflowRecord(user_id=user_id, data=data)
        return await self._storage.upsert_workflow(user_id, record)

    async def get_workflow(
        self,
        user_id: str,
        workflow_id: str,
    ) -> WorkflowRecord:
        """Fetch one workflow or answer 404.

        Args:
            user_id (`str`): The owner's user id.
            workflow_id (`str`): The workflow id.

        Returns:
            `WorkflowRecord`: The record.

        Raises:
            `HTTPException`: 404 when not found.
        """
        record = await self._storage.get_workflow(user_id, workflow_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id!r} not found.",
            )
        return record

    async def list_workflows(self, user_id: str) -> list[WorkflowRecord]:
        """Every workflow the user owns.

        Args:
            user_id (`str`): The owner's user id.

        Returns:
            `list[WorkflowRecord]`: The records, oldest first.
        """
        return await self._storage.list_workflows(user_id)

    async def update_workflow(
        self,
        user_id: str,
        workflow_id: str,
        data: WorkflowData,
    ) -> WorkflowRecord:
        """Validate and overwrite a workflow, bumping its version.

        Args:
            user_id (`str`): The owner's user id.
            workflow_id (`str`): The workflow id.
            data (`WorkflowData`): The new content.

        Returns:
            `WorkflowRecord`: The updated record.

        Raises:
            `HTTPException`: 404 when not found; 422 when invalid.
        """
        record = await self.get_workflow(user_id, workflow_id)
        problems = await self._validate(user_id, data.graph)
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"problems": problems},
            )
        record.data = data.model_copy(
            update={"version": record.data.version + 1},
        )
        return await self._storage.upsert_workflow(user_id, record)

    async def delete_workflow(self, user_id: str, workflow_id: str) -> None:
        """Delete a workflow or answer 404.

        Args:
            user_id (`str`): The owner's user id.
            workflow_id (`str`): The workflow id.

        Raises:
            `HTTPException`: 404 when not found.
        """
        record = await self.get_workflow(user_id, workflow_id)
        await self._storage.delete_workflow(user_id, record.id)

    async def _validate(
        self,
        user_id: str,
        graph: WorkflowGraph,
    ) -> list[str]:
        """Structural checks plus agent existence.

        Args:
            user_id (`str`): The owner whose agents are referenced.
            graph (`WorkflowGraph`): The graph to check.

        Returns:
            `list[str]`: Human-readable problems; empty means valid.
        """
        problems = validate_workflow_graph(graph)
        agent_ids = {
            node.config.agent_id
            for node in graph.nodes
            if isinstance(node.config, AgentNodeConfig)
        }
        agent_ids.update(
            node.config.body_agent_id
            for node in graph.nodes
            if isinstance(node.config, LoopNodeConfig)
            and node.config.body_agent_id
        )
        for agent_id in sorted(agent_ids):
            if await self._storage.get_agent(user_id, agent_id) is None:
                problems.append(
                    f"Node references unknown agent {agent_id!r}",
                )
        return problems
