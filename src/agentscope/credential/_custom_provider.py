# -*- coding: utf-8 -*-
"""A user-defined OpenAI-compatible credential."""
from typing import Literal, Type, TYPE_CHECKING

from pydantic import ConfigDict, Field, SecretStr

from ._base import CredentialBase

if TYPE_CHECKING:
    from ..model import ChatModelBase


class CustomProviderCredential(CredentialBase):
    """A user-defined OpenAI-compatible endpoint.

    Collects everything a self-hosted or third-party OpenAI-compatible
    deployment needs in one place: the API key, the base URL it serves,
    and the model names to offer in the model pickers. Chat runs go
    through :class:`~agentscope.model.OpenAIChatModel` with this base
    URL, so any endpoint implementing the OpenAI chat-completions API
    works (vLLM, LiteLLM, Ollama's ``/v1``, one-api, ...).
    """

    model_config = ConfigDict(
        title="Custom Provider (OpenAI-compatible)",
    )

    type: Literal["custom_credential"] = "custom_credential"
    """The credential type."""

    api_key: SecretStr = Field(
        description=(
            "The API key. Endpoints that ignore authorization accept "
            "any non-empty value."
        ),
    )
    """The API key."""

    base_url: str = Field(
        description=(
            "The OpenAI-compatible base URL, e.g. "
            "``http://localhost:8000/v1``."
        ),
    )
    """The base URL the endpoint serves."""

    organization: str | None = Field(
        default=None,
        description=(
            "Optional organization header, when the endpoint uses one."
        ),
    )
    """The organization header (``OpenAIChatModel`` reads it)."""

    models: list[str] = Field(
        min_length=1,
        description=(
            "Model names this endpoint serves; each becomes a "
            "selectable model in the pickers."
        ),
    )
    """The user-declared model names."""

    @classmethod
    def get_chat_model_class(cls) -> Type["ChatModelBase"]:
        """Return the OpenAI chat model class."""
        from ..model import OpenAIChatModel

        return OpenAIChatModel
