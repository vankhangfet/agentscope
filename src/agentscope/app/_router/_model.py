# -*- coding: utf-8 -*-
"""The model router."""

from fastapi import APIRouter, Depends, HTTPException, status

from ._schema import ListModelsResponse, ListModelsRequest
from ..deps import get_current_user_id, get_storage
from ..storage import StorageBase
from ...credential import CredentialFactory
from ...model import ModelCard

model_router = APIRouter(
    prefix="/model",
    tags=["model"],
    responses={404: {"description": "Not found"}},
)


@model_router.get(
    "/",
    response_model=ListModelsResponse,
    summary="List all candidate models under the given credential type",
)
async def list_models(
    body: ListModelsRequest = Depends(),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> ListModelsResponse:
    """Return all candidate models under the given credential type.

    With ``credential_id`` the listing is restricted to that one
    credential: custom-provider credentials answer with the model
    names they declare, other types fall back to the provider's
    built-in card list.

    Args:
        body (ListModelsRequest): The request body.
        user_id (`str`): The caller, for owner-scoping the credential.
        storage (`StorageBase`): Application storage.

    Returns:
        `ListModelsResponse`: The response body.
    """
    credential_cls = CredentialFactory.get_credential_class(body.provider)
    if credential_cls is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider '{body.provider}' not found.",
        )

    if body.credential_id is not None:
        record = await storage.get_credential(
            user_id,
            body.credential_id,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Credential {body.credential_id!r} not found.",
            )
        data = record.data
        if data.get("type") == "custom_credential":
            # Custom cards clone the OpenAI chat template so the
            # parameter schema (temperature, max_tokens, ...) stays a
            # valid, renderable shape; sizes reset to the same defaults
            # ``ChatModelBase`` keeps for nameless custom models.
            template = ModelCard.model_validate(
                credential_cls.get_chat_model_class()
                .list_models()[0]
                .model_dump(),
            )
            models = [
                template.model_copy(
                    update={
                        "name": name,
                        "label": name,
                        "context_size": 32768,
                        "output_size": 32768,
                    },
                )
                for name in data.get("models", [])
            ]
            return ListModelsResponse(models=models, total=len(models))

    if body.provider == "custom_credential":
        # No class-level card list exists — each credential declares
        # its own, answered above when ``credential_id`` is given.
        return ListModelsResponse(models=[], total=0)

    models = credential_cls.get_chat_model_class().list_models()
    return ListModelsResponse(models=models, total=len(models))
