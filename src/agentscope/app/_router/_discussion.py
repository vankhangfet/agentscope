# -*- coding: utf-8 -*-
"""Discussion router — start debates, follow them, cancel them."""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import (
    get_current_user_id,
    get_discussion_engine,
    get_storage,
)
from ..storage import DiscussionStatus, StorageBase
from ..discussion import create_and_dispatch, rerun_discussion
from ._schema import (
    CreateDiscussionRequest,
    CreateDiscussionResponse,
    DiscussionDetailResponse,
    ListDiscussionsResponse,
)
from .._service import DebateEngine

discussion_router = APIRouter(
    prefix="/discussion",
    tags=["discussion"],
    responses={404: {"description": "Not found"}},
)


def _parse_discussion_status(value: str | None) -> DiscussionStatus | None:
    """Translate the ``?status=`` query string into a DiscussionStatus.

    Args:
        value (`str | None`):
            The raw query parameter, or ``None`` when absent.

    Returns:
        `DiscussionStatus | None`:
            The matching status, or ``None`` to leave it unfiltered.

    Raises:
        `HTTPException`:
            422 listing the allowed values when *value* is not one.
    """
    if value is None:
        return None
    try:
        return DiscussionStatus(value)
    except ValueError:
        allowed = ", ".join(member.value for member in DiscussionStatus)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown status {value!r}; allowed values: {allowed}.",
        ) from None


@discussion_router.post(
    "/",
    response_model=CreateDiscussionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a discussion",
)
async def create_discussion(
    body: CreateDiscussionRequest,
    user_id: str = Depends(get_current_user_id),
    engine: DebateEngine = Depends(get_discussion_engine),
    storage: StorageBase = Depends(get_storage),
) -> CreateDiscussionResponse:
    """Validate the payload, persist it PENDING and dispatch it.

    Validation and the judge default are the facade's
    (:func:`agentscope.app.discussion.create_and_dispatch`) — one
    source of truth for every creator of discussions.

    Args:
        body (`CreateDiscussionRequest`):
            The topic, the debaters, an optional judge and the round
            ceiling.
        user_id (`str`):
            Injected authenticated user id.
        engine (`DebateEngine`):
            Injected discussion engine.
        storage (`StorageBase`):
            Injected application storage.

    Returns:
        `CreateDiscussionResponse`:
            The server-assigned identifier.

    Raises:
        `HTTPException`:
            422 when the payload fails the facade's rules (a blank
            topic, debater names that repeat) — the structural bounds
            (debater count, ``max_rounds``) were already enforced by
            the request schema.
    """
    try:
        record = await create_and_dispatch(
            user_id,
            engine=engine,
            storage=storage,
            topic=body.topic,
            debaters=body.debaters,
            judge=body.judge,
            max_rounds=body.max_rounds,
            trigger="api",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None
    return CreateDiscussionResponse(discussion_id=record.id)


@discussion_router.get(
    "/",
    response_model=ListDiscussionsResponse,
    summary="List the caller's discussions",
)
async def list_discussions(
    status: str | None = Query(
        default=None,
        description="Only discussions in this status "
        "(a DiscussionStatus value).",
    ),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> ListDiscussionsResponse:
    """Return every discussion the caller owns, newest first.

    Args:
        status (`str | None`):
            Only discussions in this status when given; one of the
            :class:`DiscussionStatus` values.
        user_id (`str`):
            Injected authenticated user id.
        storage (`StorageBase`):
            Injected application storage.

    Returns:
        `ListDiscussionsResponse`:
            The caller's discussions, newest first.

    Raises:
        `HTTPException`:
            422 when ``status`` is not a :class:`DiscussionStatus`
            value.
    """
    discussion_status = _parse_discussion_status(status)
    discussions = await storage.list_discussions(user_id, discussion_status)
    return ListDiscussionsResponse(
        discussions=discussions,
        total=len(discussions),
    )


@discussion_router.post(
    "/{discussion_id}/cancel",
    response_model=DiscussionDetailResponse,
    summary="Cancel a discussion",
)
async def cancel_discussion(
    discussion_id: str,
    user_id: str = Depends(get_current_user_id),
    engine: DebateEngine = Depends(get_discussion_engine),
) -> DiscussionDetailResponse:
    """Cancel a discussion: stop its loop, mark the record.

    Args:
        discussion_id (`str`):
            The discussion to cancel.
        user_id (`str`):
            Injected authenticated user id.
        engine (`DebateEngine`):
            Injected discussion engine.

    Returns:
        `DiscussionDetailResponse`:
            The cancelled discussion's content.

    Raises:
        `HTTPException`:
            404 when the discussion is missing.
    """
    record = await engine.cancel(user_id, discussion_id)
    return DiscussionDetailResponse(data=record.data)


@discussion_router.post(
    "/{discussion_id}/rerun",
    response_model=CreateDiscussionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Rerun a discussion",
)
async def rerun_discussion_endpoint(
    discussion_id: str,
    user_id: str = Depends(get_current_user_id),
    engine: DebateEngine = Depends(get_discussion_engine),
    storage: StorageBase = Depends(get_storage),
) -> CreateDiscussionResponse:
    """Clone a discussion's configuration into a fresh run.

    The source record is the history of a debate that already ran, so a
    rerun never mutates it: its topic, debaters, judge and round ceiling
    are copied into a new PENDING discussion that is dispatched on its
    own id (:func:`agentscope.app.discussion.rerun_discussion`). To
    rerun with changes, the client edits the configuration and creates
    an ordinary new discussion instead — this endpoint is the
    exact-copy path.

    Args:
        discussion_id (`str`):
            The source discussion to clone from.
        user_id (`str`):
            Injected authenticated user id; also scopes the lookup, so
            a caller can only rerun their own discussion.
        engine (`DebateEngine`):
            Injected discussion engine.
        storage (`StorageBase`):
            Injected application storage.

    Returns:
        `CreateDiscussionResponse`:
            The new discussion's server-assigned identifier.

    Raises:
        `HTTPException`:
            404 when the caller owns no such discussion.
    """
    try:
        record = await rerun_discussion(
            user_id,
            discussion_id,
            engine=engine,
            storage=storage,
            trigger="api",
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Discussion {discussion_id!r} not found.",
        ) from None
    return CreateDiscussionResponse(discussion_id=record.id)


# The detail route stays last: fixed segments precede ``/{discussion_id}``
# in FastAPI's order-dependent path matching (the workflow router's
# discipline), and every earlier route here is a fixed segment itself.
@discussion_router.get(
    "/{discussion_id}",
    response_model=DiscussionDetailResponse,
    summary="Read one discussion",
)
async def get_discussion(
    discussion_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> DiscussionDetailResponse:
    """Return one discussion's persisted state.

    Args:
        discussion_id (`str`):
            The discussion to read.
        user_id (`str`):
            Injected authenticated user id.
        storage (`StorageBase`):
            Injected application storage.

    Returns:
        `DiscussionDetailResponse`:
            The discussion's content.

    Raises:
        `HTTPException`:
            404 when the caller owns no such discussion.
    """
    record = await storage.get_discussion(user_id, discussion_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Discussion {discussion_id!r} not found.",
        )
    return DiscussionDetailResponse(data=record.data)
