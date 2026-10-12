# -*- coding: utf-8 -*-
"""Service layer for the AgentScope app."""
from ._access import (
    AgentView,
    CredentialView,
    KnowledgeBaseStatusCounts,
    KnowledgeBaseView,
    ResourceAccessService,
)
from ._channel import ChannelService
from ._credential_binding import (
    CredentialBindingError,
    CredentialBindingService,
)
from ._chat import ChatService
from ._embedding import get_embedding_model
from ._index_sweeper import IndexSweeper
from ._index_task_consumer import IndexTaskConsumer
from ._index_worker import IndexWorker
from ._knowledge_base import KnowledgeBaseService
from ._mcp_presets import (
    PRESETS,
    IntegrationPreset,
    PresetField,
    get_preset,
    preset_view,
    render_preset,
)
from ._mcp_render import MCPRenderError, render_mcp
from ._model import get_model, get_realtime_model
from ._tts_model import get_tts_model
from ._session import SessionService, SessionStatus
from ._session_projection import SessionProjection
from ._projectors import SubagentHitlProjector
from ._realtime import RealtimeService
from ._sop import SessionSOPStep, SOPService
from ._toolkit import get_toolkit
from ._download_token import sign_download_token, verify_download_token
from ._workflow import WorkflowService
from ._workflow_engine import WorkflowEngine, make_judge_agent_factory
from ._workspace import GitStatus, WorkspaceService, WorkspaceStatus

__all__ = [
    "AgentView",
    "ChannelService",
    "CredentialBindingService",
    "CredentialBindingError",
    "ChatService",
    "CredentialView",
    "GitStatus",
    "IndexSweeper",
    "IndexTaskConsumer",
    "IndexWorker",
    "KnowledgeBaseService",
    "KnowledgeBaseStatusCounts",
    "KnowledgeBaseView",
    "MCPRenderError",
    "PRESETS",
    "IntegrationPreset",
    "PresetField",
    "ResourceAccessService",
    "RealtimeService",
    "SessionSOPStep",
    "SOPService",
    "SessionService",
    "SessionStatus",
    "SessionProjection",
    "SubagentHitlProjector",
    "WorkspaceService",
    "WorkflowEngine",
    "WorkflowService",
    "make_judge_agent_factory",
    "sign_download_token",
    "verify_download_token",
    "WorkspaceStatus",
    "get_embedding_model",
    "get_model",
    "get_preset",
    "get_realtime_model",
    "get_tts_model",
    "get_toolkit",
    "preset_view",
    "render_mcp",
    "render_preset",
]
