# -*- coding: utf-8 -*-
"""Credential router — CRUD endpoints for API key credentials."""
import asyncio
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, status

from ..access import ResourceKind
from ..deps import (
    get_current_user_id,
    get_resource_access_service,
    get_storage,
)
from ._schema import (
    CreateCredentialRequest,
    CreateCredentialResponse,
    ListCredentialsResponse,
    ListCredentialSchemasResponse,
    TestCredentialRequest,
    TestCredentialResult,
    UpdateCredentialRequest,
)
from .._service import CredentialView, ResourceAccessService, get_model
from ..storage import ChatModelConfig, StorageBase
from ...credential import CredentialFactory
from ...message import UserMsg
from ...model import ChatModelBase

credential_router = APIRouter(
    prefix="/credential",
    tags=["credential"],
    responses={404: {"description": "Not found"}},
)

_TEST_CALL_TIMEOUT_SECONDS = 15.0
"""How long the single credential-test model call may run."""


async def _call_model_once(model: ChatModelBase) -> None:
    """Make one minimal model call, draining streamed replies.

    ``ChatModelBase.__call__`` returns either the full response or,
    for streaming models, an async generator whose final chunk carries
    the complete content — both shapes mean "the model replied".
    """
    result = await model([UserMsg(name="user", content="Reply with OK.")])
    if isinstance(result, AsyncGenerator):
        async for _chunk in result:
            pass


@credential_router.get(
    "/schemas",
    response_model=ListCredentialSchemasResponse,
    summary="List JSON schemas for all credential types",
)
async def list_credential_schemas() -> ListCredentialSchemasResponse:
    """Return JSON schemas for all registered credential types.

    Used by the frontend to render credential creation forms dynamically.
    """

    return ListCredentialSchemasResponse(
        schemas=CredentialFactory.list_schemas(),
    )


@credential_router.get(
    "/",
    response_model=ListCredentialsResponse,
    summary="List all credentials",
)
async def list_credentials(
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> ListCredentialsResponse:
    """Return all credential records visible to the authenticated user.

    Includes the caller's own credentials plus any credentials shared to
    them through :class:`ResourceAccessPolicyBase`. Shared entries have
    their secret ``data`` payload masked — only the discriminator and
    display name survive in the response — while runtime resolution
    (e.g. the chat model service) still sees the full payload.

    Args:
        user_id (`str`):
            Injected authenticated user ID.
        access (`ResourceAccessService`):
            Injected resource access service.

    Returns:
        `ListCredentialsResponse`:
            All visible credentials paired with editability and
            (for shared entries) redacted data.
    """
    entries = await access.list_resource(user_id, ResourceKind.CREDENTIAL)
    return ListCredentialsResponse(
        credentials=entries,
        total=len(entries),
    )


@credential_router.post(
    "/",
    response_model=CreateCredentialResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new credential",
)
async def create_credential(
    body: CreateCredentialRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> CreateCredentialResponse:
    """Store a new credential.

    Args:
        body (`CreateCredentialRequest`): Credential payload to store.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Returns:
        `CreateCredentialResponse`: The server-assigned credential identifier.
    """
    credential_id = await storage.upsert_credential(
        user_id,
        CredentialFactory.from_dict(body.data),
    )
    return CreateCredentialResponse(credential_id=credential_id)


@credential_router.patch(
    "/{credential_id}",
    response_model=CredentialView,
    summary="Update a credential",
)
async def update_credential(
    credential_id: str,
    body: UpdateCredentialRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> CredentialView:
    """Replace the payload of an existing credential.

    Args:
        credential_id (`str`): The credential to update.
        body (`UpdateCredentialRequest`): New credential payload.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.
        access (`ResourceAccessService`): Injected access service — used
            to resolve the owning user and enforce the edit permission
            when a shared editor updates the credential.

    Returns:
        `CredentialView`: The updated credential record.

    Raises:
        `HTTPException`: 404 if the credential is not visible to the
            caller; 403 if visible but only readable.
    """
    owner_id, _ = await access.resolve_for_edit(
        user_id,
        ResourceKind.CREDENTIAL,
        credential_id,
    )

    credential = CredentialFactory.from_dict(body.data)
    credential.id = credential_id
    await storage.upsert_credential(owner_id, credential)
    # ``resolve_for_edit`` proved the record existed under ``owner_id``
    # and the upsert above just wrote back to the same key, so the read
    # is a value refresh, not an existence check. If it still comes back
    # empty (e.g. a concurrent delete), surface an explicit server error
    # rather than relying on ``assert`` (which ``-O`` strips).
    updated = await storage.get_credential(owner_id, credential_id)
    if updated is None:
        raise RuntimeError(
            f"Credential {credential_id!r} for owner {owner_id!r} "
            "disappeared immediately after a successful upsert.",
        )
    # Only reachable via ``resolve_for_edit``, so the caller has edit
    # permission by construction.
    return CredentialView.model_validate(
        {**updated.model_dump(), "editable": True},
    )


@credential_router.delete(
    "/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a credential",
)
async def delete_credential(
    credential_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> None:
    """Permanently delete a credential.

    Args:
        credential_id (`str`): The credential to delete.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.
        access (`ResourceAccessService`): Injected access service — used
            to resolve the owning user and enforce the edit permission
            when a shared editor deletes the credential.

    Raises:
        `HTTPException`: 404 if the credential is not visible to the
            caller; 403 if visible but only readable.
    """
    owner_id, _ = await access.resolve_for_edit(
        user_id,
        ResourceKind.CREDENTIAL,
        credential_id,
    )
    await storage.delete_credential(owner_id, credential_id)


@credential_router.post(
    "/{credential_id}/test",
    response_model=TestCredentialResult,
    summary="Test a credential with one minimal model call",
)
async def test_credential(
    credential_id: str,
    body: TestCredentialRequest,
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> TestCredentialResult:
    """Build a model from a stored credential and call it once.

    Provider, authentication and network failures are data, not server
    errors: they come back as ``ok=false`` with the error text, so the
    endpoint never answers 5xx for a broken credential.

    Args:
        credential_id (`str`): The credential to exercise.
        body (`TestCredentialRequest`): Model name and parameters for
            the single test call.
        user_id (`str`): Injected authenticated user ID.
        access (`ResourceAccessService`): Injected access service —
            resolves the credential with owner-or-shared semantics, so
            a read-only shared credential is testable too.

    Returns:
        `TestCredentialResult`: Whether the call succeeded and what
        happened.

    Raises:
        `HTTPException`: 404 if the credential is not visible to the
            caller.
    """
    record = await access.resolve_credential(user_id, credential_id)

    config = ChatModelConfig(
        type=record.data["type"],
        credential_id=credential_id,
        model=body.model,
        parameters=body.parameters,
    )

    try:
        model = await get_model(user_id, config, access)
        # ``ChatModelBase.__call__`` answers a cancellation with an
        # INTERRUPTED reply instead of raising, which swallows the
        # cancellation ``asyncio.wait_for`` needs to see before it can
        # report ``TimeoutError``. Race the call as its own task and
        # cancel the loser explicitly instead.
        call = asyncio.create_task(_call_model_once(model))
        try:
            _, pending = await asyncio.wait(
                {call},
                timeout=_TEST_CALL_TIMEOUT_SECONDS,
            )
            if pending:
                return TestCredentialResult(
                    ok=False,
                    detail=(
                        "Model call timed out after "
                        f"{_TEST_CALL_TIMEOUT_SECONDS:g}s."
                    ),
                )
            await call
        finally:
            # Contain the child task on every exit path — including a
            # cancellation of this handler itself, which would
            # otherwise leak the model call to natural completion (a
            # hung provider can hold it for the SDK's own timeout).
            if not call.done():
                call.cancel()
            # ``asyncio.wait`` reaps without re-raising, so the task
            # settles no matter how its ``__call__`` handled the
            # cancellation.
            await asyncio.wait({call})
            # Retrieve (and discard) a stray exception so a task that
            # errored right around the deadline doesn't trip "Task
            # exception was never retrieved" at GC time. Guarded: a
            # cancelled task raises from ``exception()``.
            if not call.cancelled():
                call.exception()
    except Exception as exc:  # pylint: disable=broad-except
        return TestCredentialResult(ok=False, detail=str(exc))
    return TestCredentialResult(ok=True, detail="Model replied successfully.")
