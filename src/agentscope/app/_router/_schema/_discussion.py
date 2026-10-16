# -*- coding: utf-8 -*-
"""Request / response schemas for the discussion router."""
from pydantic import BaseModel, Field

from ...storage import (
    DebaterConfig,
    DiscussionData,
    DiscussionRecord,
    JudgeConfig,
)


class CreateDiscussionRequest(BaseModel):
    """Request body for starting a discussion."""

    topic: str = Field(description="The issue under debate; non-empty.")

    debaters: list[DebaterConfig] = Field(
        min_length=1,
        max_length=6,
        description="Who argues — 1 to 6, each a name (unique within "
        "the discussion), a stance and a model.",
    )

    judge: JudgeConfig | None = Field(
        default=None,
        description="The evaluator; None keeps the default neutral "
        "prompt and runs the judge on the first debater's model.",
    )

    max_rounds: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Round ceiling; the debate may stop earlier on "
        "judge consensus.",
    )


class CreateDiscussionResponse(BaseModel):
    """Response body after starting a discussion."""

    discussion_id: str = Field(description="Server-assigned discussion id.")


class ListDiscussionsResponse(BaseModel):
    """The caller's discussions.

    Reflects whatever ``status`` filter the request carried; ``total``
    counts the discussions actually returned.
    """

    discussions: list[DiscussionRecord] = Field(
        description="The discussions, newest first.",
    )

    total: int = Field(description="How many there are.")


class DiscussionDetailResponse(BaseModel):
    """One discussion's persisted state."""

    data: DiscussionData = Field(description="The discussion's content.")
