# -*- coding: utf-8 -*-
"""Request / response schemas for the factory router."""
from datetime import datetime

from pydantic import BaseModel, Field

from ...storage import (
    ChatModelConfig,
    FactoryDecision,
    FactoryMode,
    FactoryPhase,
    FactoryStatus,
    FactoryTask,
    FactoryTicketRecord,
)


class CreateFactoryTicketRequest(BaseModel):
    """Request body for opening a factory ticket."""

    title: str = Field(
        min_length=1,
        description="The human's problem, in one line; non-empty.",
    )

    description: str = Field(
        default="",
        description="The problem statement the factory works from; "
        "may be empty when the title says it all.",
    )

    chat_model_config: ChatModelConfig = Field(
        description="Every role's model (v1: one per ticket).",
    )

    mode: FactoryMode = Field(
        default=FactoryMode.COLLABORATIVE,
        description="Who approves the gates: a human every time "
        "(COLLABORATIVE) or auto-approve except the final gate "
        "(DELEGATED).",
    )


class CreateFactoryTicketResponse(BaseModel):
    """Response body after opening a factory ticket."""

    ticket_id: str = Field(description="Server-assigned ticket id.")


class ApproveTicketRequest(BaseModel):
    """Request body for approving a gate."""

    note: str = Field(
        default="",
        description="The human's note, logged on the gated decision.",
    )


class RejectTicketRequest(BaseModel):
    """Request body for rejecting a gate."""

    note: str = Field(
        default="",
        description="Why the phase re-runs, logged with the rejection.",
    )


class FactoryTicketView(BaseModel):
    """One factory ticket, as the API shows it.

    The full ``data`` projection plus the record's own ids — the
    persisted fields verbatim, model config included (its
    ``credential_id`` is an id, not a secret, so it rides along like
    every debater's config in the discussion views).
    """

    ticket_id: str = Field(description="The ticket's record id.")

    title: str = Field(description="The human's problem, in one line.")

    description: str = Field(
        description="The problem statement the factory works from.",
    )

    mode: FactoryMode = Field(description="Who approves the gates.")

    phase: FactoryPhase = Field(
        description="Which phase the ticket is in.",
    )

    status: FactoryStatus = Field(description="The ticket's lifecycle.")

    chat_model_config: ChatModelConfig = Field(
        description="Every role's model (v1: one per ticket).",
    )

    discussions: dict[FactoryPhase, str] = Field(
        description="phase → discussion id: the discussion that ran "
        "(or, after a reject, the fresh re-run) for that phase.",
    )

    requirements: str | None = Field(
        description="The requirements artifact, once phase 1 concludes.",
    )

    architecture: str | None = Field(
        description="The architecture artifact, once phase 2 concludes.",
    )

    review: str | None = Field(
        description="The reviewer's output, once the tasks are done.",
    )

    tasks: list[FactoryTask] = Field(
        description="The phase 3 task breakdown with each task's own "
        "lifecycle.",
    )

    decisions: list[FactoryDecision] = Field(
        description="The decision log: every conclusion, gate note and "
        "risk noted on the ticket's way through.",
    )

    workspace_id: str | None = Field(
        description="The per-ticket workspace, minted when DEVELOP "
        "starts.",
    )

    error: str | None = Field(
        description="Why the ticket failed, when it did.",
    )

    trigger: str = Field(description="What started the ticket.")

    created_at: datetime = Field(description="When the ticket opened.")

    updated_at: datetime = Field(
        description="When the ticket last changed.",
    )

    finished_at: datetime | None = Field(
        description="Set once the ticket reaches a terminal state.",
    )

    @classmethod
    def from_record(cls, record: FactoryTicketRecord) -> "FactoryTicketView":
        """Project a stored record onto its API view.

        Args:
            record (`FactoryTicketRecord`):
                The stored ticket.

        Returns:
            `FactoryTicketView`:
                The ticket as the API shows it.
        """
        return cls(
            ticket_id=record.id,
            title=record.data.title,
            description=record.data.description,
            mode=record.data.mode,
            phase=record.data.phase,
            status=record.data.status,
            chat_model_config=record.data.chat_model_config,
            discussions=dict(record.data.discussions),
            requirements=record.data.requirements,
            architecture=record.data.architecture,
            review=record.data.review,
            tasks=list(record.data.tasks),
            decisions=list(record.data.decisions),
            workspace_id=record.data.workspace_id,
            error=record.data.error,
            trigger=record.data.trigger,
            created_at=record.created_at,
            updated_at=record.updated_at,
            finished_at=record.data.finished_at,
        )


class ListFactoryTicketsResponse(BaseModel):
    """The caller's factory tickets.

    Reflects whatever ``status`` filter the request carried; ``total``
    counts the tickets actually returned.
    """

    tickets: list[FactoryTicketView] = Field(
        description="The tickets, newest first.",
    )

    total: int = Field(description="How many there are.")


class FactoryTicketDetailResponse(BaseModel):
    """One factory ticket's projected state."""

    data: FactoryTicketView = Field(description="The ticket's view.")


class FactoryRoleView(BaseModel):
    """One factory role, as the roster shows it."""

    name: str = Field(
        description="The role's agent-safe name — the name it runs "
        "under.",
    )

    title: str = Field(description="The role's display title.")

    stance: str = Field(
        description="What the role argues for (debates) or does "
        "(development), written for display.",
    )


class FactoryRoleGroupView(BaseModel):
    """One phase's roles, as the roster shows it."""

    phase: FactoryPhase = Field(
        description="The phase the group serves.",
    )

    title: str = Field(description="The group's display title.")

    description: str = Field(
        description="One line: what the group produces.",
    )

    roles: list[FactoryRoleView] = Field(
        description="The group's roles, in speaking order.",
    )


class FactoryRosterResponse(BaseModel):
    """The factory's whole team, as the roster shows it.

    The static role catalog every ticket runs — not any ticket's
    state — with the judge alongside because both debate phases
    share it.
    """

    groups: list[FactoryRoleGroupView] = Field(
        description="The role groups, in phase order.",
    )

    judge: FactoryRoleView = Field(
        description="The judge both debate phases share.",
    )
