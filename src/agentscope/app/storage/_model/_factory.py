# -*- coding: utf-8 -*-
"""The software-factory models: a ticket's configuration plus the
phases it walks through.

A ticket is the human's problem statement; everything the factory
does with it — which phase it is in, which discussion argued that
phase, the artifacts and decisions so far — is data on the record,
so the engine stays a pure walker over this shape.
"""
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ._base import _RecordBase
from ._session import ChatModelConfig


class FactoryMode(str, Enum):
    """How the ticket's gates are handled."""

    COLLABORATIVE = "COLLABORATIVE"
    """A human approves or rejects every gate."""

    DELEGATED = "DELEGATED"
    """Gates auto-approve except the final one."""


class FactoryPhase(str, Enum):
    """The phases a ticket walks through, in order."""

    REQUIREMENTS = "REQUIREMENTS"
    """Multi-agent discussion produces the requirements artifact."""

    ARCHITECTURE = "ARCHITECTURE"
    """Multi-agent discussion produces the architecture artifact."""

    DEVELOP = "DEVELOP"
    """Agent turns break down and execute the tasks, then review."""


class FactoryStatus(str, Enum):
    """Where a ticket stands."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FactoryTask(BaseModel):
    """One development task, broken out by the Plan Lead and executed
    by a Developer turn."""

    # The engine fills status / output in place as turns run, so
    # assignments revalidate — same discipline as the run models.
    model_config = ConfigDict(validate_assignment=True)

    title: str
    """What the task delivers, in one line."""

    description: str = ""
    """The detail the Developer turn works from."""

    assignee: str = ""
    """The logical role name the task is routed to."""

    status: Literal["PENDING", "RUNNING", "DONE", "FAILED"] = "PENDING"
    """The task's own lifecycle (``DONE`` — not the ticket's
    ``COMPLETED`` — is its success state)."""

    output: str | None = None
    """What the Developer turn produced, once it has."""


class FactoryDecision(BaseModel):
    """One entry in the ticket's decision log."""

    # Appended by the engine as phases conclude and gates resolve;
    # revalidation keeps a hand-forged note from smuggling in a bad
    # phase.
    model_config = ConfigDict(validate_assignment=True)

    phase: FactoryPhase
    """The phase the decision was taken in."""

    discussion_id: str | None
    """The discussion that produced it, when one did — ``None`` for
    decisions taken outside a discussion (e.g. the final gate)."""

    conclusion: str
    """The discussion's synthesis, or the gate's resolution."""

    note: str = ""
    """The human's gate note (approve or reject)."""

    consensus: bool
    """Whether the discussion converged; a disagreement still decides
    — the human saw it and chose anyway."""

    created_at: datetime
    """When the decision landed."""


class FactoryTicketData(BaseModel):
    """A ticket's persisted content."""

    # Mutated in place by the engine hot loop (status, phase, tasks,
    # decisions), so assignments revalidate — mirroring the run and
    # discussion models.
    model_config = ConfigDict(validate_assignment=True)

    title: str
    """The human's problem, in one line."""

    description: str
    """The human's problem statement."""

    mode: FactoryMode = FactoryMode.COLLABORATIVE
    """Who approves the gates."""

    phase: FactoryPhase = FactoryPhase.REQUIREMENTS
    """Which phase the ticket is in."""

    status: FactoryStatus = FactoryStatus.PENDING
    """The ticket's lifecycle."""

    chat_model_config: ChatModelConfig
    """Every role's model (v1: one per ticket)."""

    discussions: dict[FactoryPhase, str] = Field(default_factory=dict)
    """phase → discussion id: the discussion that ran (or, after a
    reject, the fresh re-run) for that phase."""

    requirements: str | None = None
    """The requirements artifact, once phase 1 concludes."""

    architecture: str | None = None
    """The architecture artifact, once phase 2 concludes."""

    tasks: list[FactoryTask] = Field(default_factory=list)
    """The phase 3 task breakdown, filled by the Plan Lead."""

    review: str | None = None
    """The reviewer's output, once the tasks are done."""

    decisions: list[FactoryDecision] = Field(default_factory=list)
    """The decision log: every conclusion, gate note and risk noted
    on the ticket's way through."""

    workspace_id: str | None = None
    """The per-ticket workspace, minted when DEVELOP starts."""

    error: str | None = None
    """Why the ticket failed, when it did."""

    trigger: str = Field(
        default="manual",
        description="manual | api",
    )
    """What started the ticket."""

    finished_at: datetime | None = None
    """Set once the ticket reaches a terminal state."""


class FactoryTicketRecord(_RecordBase):
    """A stored factory ticket."""

    user_id: str
    """The owner's user id."""

    data: FactoryTicketData
    """The ticket's content."""
