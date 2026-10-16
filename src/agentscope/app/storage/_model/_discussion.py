# -*- coding: utf-8 -*-
"""The discussion models: a debate's configuration plus the rounds it
walked through.

Debaters and the judge are data (prompts and model configs), not
code — changing the domain a discussion argues about means changing
payloads, never the engine.
"""
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ._base import _RecordBase
from ._session import ChatModelConfig


class DiscussionStatus(str, Enum):
    """Where a discussion stands."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DebaterConfig(BaseModel):
    """One debater: everything needed to argue its side."""

    name: str
    """The debater's display name."""

    system_prompt: str
    """The stance the debater argues and the expertise it brings."""

    chat_model_config: ChatModelConfig
    """The model the debater argues through."""


class JudgeConfig(BaseModel):
    """The evaluator: scores arguments, detects consensus, and at stop
    synthesizes the conclusion."""

    system_prompt: str = (
        "You are an impartial debate judge. Assess each argument "
        "strictly on its merits — reasoning, evidence, relevance — "
        "without favoring any side, and never argue a position "
        "yourself."
    )
    """The judge's instructions; the default keeps it neutral."""

    chat_model_config: ChatModelConfig
    """The model the judge evaluates through."""


class RoundArgument(BaseModel):
    """One debater's argument in one round."""

    # The judge fills strength / rebutted / note in place after the
    # round, so assignments revalidate — same discipline as the run
    # models, so a bad value fails here rather than after the next
    # persist.
    model_config = ConfigDict(validate_assignment=True)

    debater: str
    """Which debater argued this."""

    text: str
    """The argument as delivered."""

    strength: int | None = Field(default=None, ge=1, le=5)
    """The judge's 1-5 score, once judged."""

    rebutted: bool | None = None
    """Whether a later argument beats it, once judged."""

    note: str = ""
    """The judge's one-line assessment."""


class RoundEvaluation(BaseModel):
    """The judge's verdict on one round.

    A round is persisted empty at its start and filled in place as its
    debaters deliver, so the shape means two things: on a non-terminal
    discussion, a round with fewer ``arguments`` than the discussion
    has debaters is a round still in progress — the missing arguments
    are simply not argued yet, and the judge's fields (strengths,
    ``consensus``, ``summary``) have not landed. On a terminal
    discussion the same shape reads as the round as far as it got
    before the debate stopped.
    """

    model_config = ConfigDict(validate_assignment=True)

    round: int
    """Which round this evaluates (1-based)."""

    arguments: list[RoundArgument] = Field(default_factory=list)
    """The round's arguments, in speaking order — empty while the
    round is still being argued, filling as each debater delivers."""

    consensus: bool = False
    """Whether the judge found the sides converging."""

    summary: str = ""
    """The judge's round summary."""


class DiscussionData(BaseModel):
    """A discussion's persisted content."""

    # Mutated in place by the engine hot loop (status, rounds,
    # current_speaker, conclusion), so assignments revalidate —
    # mirroring the run models.
    model_config = ConfigDict(validate_assignment=True)

    topic: str
    """The issue under debate."""

    status: DiscussionStatus = DiscussionStatus.PENDING
    """The discussion's lifecycle."""

    max_rounds: int = Field(default=3, ge=1, le=10)
    """Round ceiling; the debate may stop earlier on consensus."""

    debaters: list[DebaterConfig] = Field(min_length=1)
    """Who argues. At least one; the API caps the count at six."""

    judge: JudgeConfig
    """The evaluator."""

    rounds: list[RoundEvaluation] = Field(default_factory=list)
    """The blackboard: every finished round, in order — plus, while a
    round is being argued, that round's in-progress slot at the end
    (see :class:`RoundEvaluation`)."""

    current_speaker: str | None = None
    """Who holds the floor right now, for live progress: a debater's
    name while that debater's turn runs, ``"judge"`` while the judge
    verdicts or synthesizes, and ``None`` when nothing is speaking —
    before dispatch and on every terminal state."""

    conclusion: str | None = None
    """The final synthesis, once reached."""

    error: str | None = None
    """Why the discussion failed, when it did."""

    trigger: str = Field(
        default="manual",
        description="manual | api | chat_tool",
    )
    """What started the discussion."""

    finished_at: datetime | None = None
    """Set once the discussion reaches a terminal state."""


class DiscussionRecord(_RecordBase):
    """A stored discussion."""

    user_id: str
    """The owner's user id."""

    data: DiscussionData
    """The discussion's content."""
