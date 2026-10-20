# -*- coding: utf-8 -*-
"""The storage module in agentscope."""
from typing import TYPE_CHECKING

from ._base import StorageBase
from ._redis_storage import RedisStorage
from ._model import (
    AgentData,
    AgentRecord,
    ChannelBinding,
    ChannelRecord,
    ChunkerConfig,
    RoutingConfig,
    SessionScope,
    SessionSettings,
    CredentialRecord,
    KnowledgeBaseData,
    KnowledgeBaseRecord,
    KnowledgeDocumentData,
    KnowledgeDocumentRecord,
    KnowledgeDocumentStatus,
    MCPRecord,
    ScheduleData,
    ScheduleRecord,
    ScheduleOrigin,
    SessionConfig,
    SessionNaming,
    SessionKnowledgeConfig,
    SessionRecord,
    SessionOrigin,
    SessionSource,
    UserOrigin,
    ScheduleSource,
    ChannelOrigin,
    TeamOrigin,
    DiscussionOrigin,
    AgentVerifier,
    HumanVerifier,
    SOPAgentRef,
    SOPData,
    SOPOrigin,
    WorkflowOrigin,
    SOPRecord,
    SOPRunRecord,
    SOPStepDataV1,
    SOPVerifier,
    SOPWorkspaceGrain,
    SkillRecord,
    ChatModelConfig,
    RealtimeModelConfig,
    TTSModelConfig,
    EmbeddingModelConfig,
    TeamData,
    TeamRecord,
    UserRecord,
    TeamMember,
    AgentChatConfig,
    InviteConfig,
    AgentNodeConfig,
    ConditionBranch,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    NotifyNodeConfig,
    WorkflowData,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPosition,
    WorkflowRecord,
    NodeRunState,
    RunStatus,
    WorkflowRunData,
    WorkflowRunRecord,
    DebaterConfig,
    DiscussionData,
    DiscussionRecord,
    DiscussionStatus,
    JudgeConfig,
    RoundArgument,
    RoundEvaluation,
    FactoryDecision,
    FactoryMode,
    FactoryPhase,
    FactoryStatus,
    FactoryTask,
    FactoryTicketData,
    FactoryTicketRecord,
)

# A validation helper rather than a model, so it comes straight from its
# module instead of riding the model re-exports above.
from ._model._workflow import validate_workflow_graph

if TYPE_CHECKING:
    # Re-exported for static type checkers only; at runtime the class is
    # loaded lazily by ``__getattr__`` below so callers without the
    # optional ``sql`` extra never trigger the SQLAlchemy import that the
    # ``_sql`` package does at load time (its declarative-tables module
    # cannot defer it).
    from ._sql import AsyncSQLAlchemyStorage  # noqa: F401


def __getattr__(name: str) -> object:
    """Lazily load the optional SQL backend on first attribute access.

    Keeps ``import agentscope.app.storage`` cheap — ``StorageBase``,
    ``RedisStorage`` and the record models never need SQLAlchemy — while
    still exposing ``AsyncSQLAlchemyStorage`` from this package for
    callers that do ``from agentscope.app.storage import
    AsyncSQLAlchemyStorage``. SQLAlchemy is imported only at that point.
    """
    if name == "AsyncSQLAlchemyStorage":
        from ._sql import AsyncSQLAlchemyStorage as _AsyncSQLAlchemyStorage

        return _AsyncSQLAlchemyStorage
    raise AttributeError(
        f"module 'agentscope.app.storage' has no attribute {name!r}",
    )


__all__ = [
    "StorageBase",
    "RedisStorage",
    "AsyncSQLAlchemyStorage",
    # The ORM models
    "AgentChatConfig",
    "InviteConfig",
    "AgentData",
    "AgentRecord",
    "ChannelBinding",
    "ChannelRecord",
    "ChunkerConfig",
    "RoutingConfig",
    "SessionScope",
    "SessionSettings",
    "CredentialRecord",
    "KnowledgeBaseData",
    "KnowledgeBaseRecord",
    "KnowledgeDocumentData",
    "KnowledgeDocumentRecord",
    "KnowledgeDocumentStatus",
    "MCPRecord",
    "SessionConfig",
    "SessionNaming",
    "SessionKnowledgeConfig",
    "SessionRecord",
    "SessionOrigin",
    "SessionSource",
    "UserOrigin",
    "ScheduleOrigin",
    "ChannelOrigin",
    "TeamOrigin",
    "DiscussionOrigin",
    "AgentVerifier",
    "HumanVerifier",
    "SOPAgentRef",
    "SOPData",
    "SOPOrigin",
    "WorkflowOrigin",
    "SOPRecord",
    "SOPRunRecord",
    "SOPStepDataV1",
    "SOPVerifier",
    "SOPWorkspaceGrain",
    "SkillRecord",
    "ChatModelConfig",
    "RealtimeModelConfig",
    "TTSModelConfig",
    "EmbeddingModelConfig",
    "TeamMember",
    "TeamData",
    "TeamRecord",
    "UserRecord",
    "ScheduleData",
    "ScheduleRecord",
    "ScheduleSource",
    # The workflow graph models
    "AgentNodeConfig",
    "ConditionBranch",
    "ConditionNodeConfig",
    "HumanApprovalNodeConfig",
    "LoopNodeConfig",
    "NotifyNodeConfig",
    "WorkflowData",
    "WorkflowEdge",
    "WorkflowGraph",
    "WorkflowNode",
    "WorkflowPosition",
    "WorkflowRecord",
    # The workflow run models
    "NodeRunState",
    "RunStatus",
    "WorkflowRunData",
    "WorkflowRunRecord",
    # The discussion models
    "DebaterConfig",
    "JudgeConfig",
    "RoundArgument",
    "RoundEvaluation",
    "DiscussionData",
    "DiscussionStatus",
    "DiscussionRecord",
    # The factory models
    "FactoryMode",
    "FactoryPhase",
    "FactoryStatus",
    "FactoryTask",
    "FactoryDecision",
    "FactoryTicketData",
    "FactoryTicketRecord",
    "validate_workflow_graph",
]
