# -*- coding: utf-8 -*-
"""Workflow service: CRUD plus the checks that need other records."""
from typing import Any

from fastapi import HTTPException, status

from ..storage import (
    AgentNodeConfig,
    ChatModelConfig,
    ConditionNodeConfig,
    LoopNodeConfig,
    RunStatus,
    StorageBase,
    WorkflowData,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRecord,
    WorkflowRunData,
    WorkflowRunRecord,
    validate_workflow_graph,
)
from ._workflow_nodes import _JUDGE_RULE_PREFIX


def _credential_problem(credential_id: str, node_id: str | None) -> str:
    """The problem text for a credential no model resolved.

    Args:
        credential_id (`str`): The unknown credential id.
        node_id (`str | None`): The node whose model referenced it, or
            ``None`` when the run-level default did.

    Returns:
        `str`: The human-readable problem.
    """
    if node_id is None:
        return (
            f"Run-level model references unknown credential "
            f"{credential_id!r}."
        )
    return (
        f"Model for node {node_id!r} references unknown credential "
        f"{credential_id!r}."
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

    async def start_run(
        self,
        user_id: str,
        workflow_id: str,
        input: dict[str, Any],  # pylint: disable=redefined-builtin
        chat_model_config: ChatModelConfig | None = None,
        trigger: str = "manual",
    ) -> WorkflowRunRecord:
        """Snapshot a workflow into a new run, validating first.

        Everything the engine relies on is checked here, before a run
        exists: every referenced agent exists and resolves to a model
        (its template's, else the run-level default), and every
        distinct credential those models reference exists — the Plan 1
        debt repaid here, where models are resolved.

        Args:
            user_id (`str`): The owner's user id.
            workflow_id (`str`): The workflow to snapshot.
            input (`dict[str, Any]`): The run's input, seeded onto the
                blackboard.
            chat_model_config (`ChatModelConfig | None`): Run-level
                default model for nodes without their own.
            trigger (`str`): What started the run: ``manual``,
                ``schedule``, ``api`` or ``chat_tool``.

        Returns:
            `WorkflowRunRecord`: The persisted run, ``PENDING`` until
            the engine dispatches it.

        Raises:
            `HTTPException`: 404 when the workflow is missing; 422
                listing every problem at once.
        """
        record = await self.get_workflow(user_id, workflow_id)
        problems: list[str] = []
        # credential id -> the node that first brought it into play
        # (None = the run-level default); one existence check each.
        credential_owners: dict[str, str | None] = {}
        if chat_model_config is not None:
            credential_owners[chat_model_config.credential_id] = None
        for node in record.data.graph.nodes:
            for model in await self._resolve_node_models(
                user_id,
                node,
                chat_model_config,
                problems,
            ):
                credential_owners.setdefault(model.credential_id, node.id)
        for credential_id, node_id in credential_owners.items():
            if (
                await self._storage.get_credential(user_id, credential_id)
                is None
            ):
                problems.append(
                    _credential_problem(credential_id, node_id),
                )
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"problems": problems},
            )
        run = WorkflowRunRecord(
            user_id=user_id,
            data=WorkflowRunData(
                workflow_id=record.id,
                workflow_version=record.data.version,
                workflow_name=record.data.name,
                graph=record.data.graph,
                input=input,
                status=RunStatus.PENDING,
                chat_model_config=chat_model_config,
                trigger=trigger,
            ),
        )
        return await self._storage.upsert_workflow_run(user_id, run)

    async def _resolve_node_models(
        self,
        user_id: str,
        node: WorkflowNode,
        run_default: ChatModelConfig | None,
        problems: list[str],
    ) -> list[ChatModelConfig]:
        """The effective models *node* will run with.

        Mirrors the handlers in :mod:`._workflow_nodes`: agent nodes
        and loop bodies take the agent template's model, else the
        run-level default; a condition node with an ``llm_judge``
        branch takes the node's model, else the run-level default; a
        rule-only condition or an approval node needs none.

        Args:
            user_id (`str`): The owner whose agents are referenced.
            node (`WorkflowNode`): The node to resolve models for.
            run_default (`ChatModelConfig | None`): The run-level
                default model.
            problems (`list[str]`): Problems collected so far; this
                node's problems, if any, are appended.

        Returns:
            `list[ChatModelConfig]`: The models the node resolved to;
            a node that recorded a problem contributes none.
        """
        cfg = node.config
        if isinstance(cfg, ConditionNodeConfig):
            if any(
                branch.rule.startswith(_JUDGE_RULE_PREFIX)
                for branch in cfg.branches
            ):
                model = cfg.chat_model_config or run_default
                if model is None:
                    problems.append(
                        f"Node {node.id!r} has no model for its "
                        "llm_judge rule: set one on the node or pass "
                        "a run-level default.",
                    )
                    return []
                return [model]
            return []
        agent_ids: list[str] = []
        if isinstance(cfg, AgentNodeConfig):
            agent_ids.append(cfg.agent_id)
        elif isinstance(cfg, LoopNodeConfig) and cfg.body_agent_id:
            # Same collection rule as ``_validate``: an iterate loop
            # with no body agent contributes nothing here and fails
            # at dispatch instead.
            agent_ids.append(cfg.body_agent_id)
        models: list[ChatModelConfig] = []
        for agent_id in agent_ids:
            agent = await self._storage.get_agent(user_id, agent_id)
            if agent is None:
                problems.append(
                    f"Node {node.id!r} references unknown agent "
                    f"{agent_id!r}.",
                )
                continue
            model = agent.data.chat_config.chat_model_config
            if model is None:
                model = run_default
            if model is None:
                problems.append(
                    f"Node {node.id!r} has no model: set one on its "
                    "agent template or pass a run-level default.",
                )
                continue
            models.append(model)
        return models
