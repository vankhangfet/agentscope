# -*- coding: utf-8 -*-
"""The chat model configuration, used as DTO layer."""

from pydantic import BaseModel, Field

from ....model import ModelCard


class ListModelsResponse(BaseModel):
    """List the candidate models response."""

    models: list[ModelCard] = Field(description="The candidate models.")
    total: int = Field(description="The total number of candidates.")


class ListModelsRequest(BaseModel):
    """List the candidate models request."""

    provider: str = Field(
        description="The provider type, e.g. openai, dashscope, etc.",
    )

    credential_id: str | None = Field(
        default=None,
        description=(
            "Restrict the listing to one credential of that provider "
            "type — custom-provider credentials declare their own model "
            "names, so each returns its own list."
        ),
    )
