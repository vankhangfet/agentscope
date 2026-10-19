# -*- coding: utf-8 -*-
"""Factory router — open tickets, follow them, answer their gates."""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import (
    get_current_user_id,
    get_factory_engine,
    get_storage,
)
from ..storage import (
    FactoryStatus,
    FactoryTicketData,
    FactoryTicketRecord,
    StorageBase,
)
from .._service import FactoryEngine
from .._service.factory._roles import JUDGE_ROLE, factory_roster
from ._schema import (
    ApproveTicketRequest,
    CreateFactoryTicketRequest,
    CreateFactoryTicketResponse,
    FactoryRoleGroupView,
    FactoryRoleView,
    FactoryRosterResponse,
    FactoryTicketDetailResponse,
    FactoryTicketView,
    ListFactoryTicketsResponse,
    RejectTicketRequest,
)

factory_router = APIRouter(
    prefix="/factory",
    tags=["factory"],
    responses={404: {"description": "Not found"}},
)


def _parse_factory_status(value: str | None) -> FactoryStatus | None:
    """Translate the ``?status=`` query string into a FactoryStatus.

    Args:
        value (`str | None`):
            The raw query parameter, or ``None`` when absent.

    Returns:
        `FactoryStatus | None`:
            The matching status, or ``None`` to leave it unfiltered.

    Raises:
        `HTTPException`:
            422 listing the allowed values when *value* is not one.
    """
    if value is None:
        return None
    try:
        return FactoryStatus(value)
    except ValueError:
        allowed = ", ".join(member.value for member in FactoryStatus)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown status {value!r}; allowed values: {allowed}.",
        ) from None


@factory_router.post(
    "/",
    response_model=CreateFactoryTicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a factory ticket",
)
async def create_factory_ticket(
    body: CreateFactoryTicketRequest,
    user_id: str = Depends(get_current_user_id),
    engine: FactoryEngine = Depends(get_factory_engine),
    storage: StorageBase = Depends(get_storage),
) -> CreateFactoryTicketResponse:
    """Validate the payload, persist it PENDING and dispatch it.

    The ticket is durable before the walker is dispatched, so a crash
    in between leaves a PENDING ticket the boot sweep fails out
    honestly. The model config's credential is not re-checked here:
    the chat path resolves it owner-scoped at turn time (the
    discussion create's discipline), and a ticket referencing a
    foreign credential fails its first phase honestly.

    Args:
        body (`CreateFactoryTicketRequest`):
            The problem statement, the model every role shares and
            the gate mode.
        user_id (`str`):
            Injected authenticated user id.
        engine (`FactoryEngine`):
            Injected factory engine.
        storage (`StorageBase`):
            Injected application storage.

    Returns:
        `CreateFactoryTicketResponse`:
            The server-assigned identifier.

    Raises:
        `HTTPException`:
            422 when the title is blank (the structural bounds — a
            non-empty title, a known mode, a model config — were
            already enforced by the request schema).
    """
    if not body.title.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="title must be a non-empty string.",
        )
    record = await storage.upsert_factory_ticket(
        user_id,
        FactoryTicketRecord(
            user_id=user_id,
            data=FactoryTicketData(
                title=body.title,
                description=body.description,
                mode=body.mode,
                chat_model_config=body.chat_model_config,
                trigger="api",
            ),
        ),
    )
    await engine.dispatch(user_id, record.id)
    return CreateFactoryTicketResponse(ticket_id=record.id)


@factory_router.get(
    "/",
    response_model=ListFactoryTicketsResponse,
    summary="List the caller's tickets",
)
async def list_factory_tickets(
    status: str | None = Query(
        default=None,
        description="Only tickets in this status "
        "(a FactoryStatus value).",
    ),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> ListFactoryTicketsResponse:
    """Return every ticket the caller owns, newest first.

    Args:
        status (`str | None`):
            Only tickets in this status when given; one of the
            :class:`FactoryStatus` values.
        user_id (`str`):
            Injected authenticated user id.
        storage (`StorageBase`):
            Injected application storage.

    Returns:
        `ListFactoryTicketsResponse`:
            The caller's tickets, newest first.

    Raises:
        `HTTPException`:
            422 when ``status`` is not a :class:`FactoryStatus`
            value.
    """
    factory_status = _parse_factory_status(status)
    tickets = await storage.list_factory_tickets(user_id, factory_status)
    views = [FactoryTicketView.from_record(record) for record in tickets]
    return ListFactoryTicketsResponse(
        tickets=views,
        total=len(views),
    )


# Declared before the ``/{ticket_id}`` routes, like the presets listing
# in the MCP router: a literal path segment must not risk being
# captured as someone's ticket id.
@factory_router.get(
    "/roles",
    response_model=FactoryRosterResponse,
    summary="Read the agent roster",
)
async def get_factory_roles() -> FactoryRosterResponse:
    """Return the factory's static role catalog: every phase's roles
    and the judge both debate phases share.

    Compiled catalog data with nothing per-user in it — the role
    groups themselves, not any ticket's state — so it takes no user,
    no storage and no engine, exactly like the MCP presets listing.

    Returns:
        `FactoryRosterResponse`:
            The role groups in phase order, plus the judge.
    """
    return FactoryRosterResponse(
        groups=[
            FactoryRoleGroupView(**group) for group in factory_roster()
        ],
        judge=FactoryRoleView(**JUDGE_ROLE),
    )


@factory_router.post(
    "/{ticket_id}/approve",
    response_model=FactoryTicketDetailResponse,
    summary="Approve a gate",
)
async def approve_factory_ticket(
    ticket_id: str,
    body: ApproveTicketRequest,
    user_id: str = Depends(get_current_user_id),
    engine: FactoryEngine = Depends(get_factory_engine),
) -> FactoryTicketDetailResponse:
    """Answer a gate with approval: advance, or complete.

    The two intermediate gates advance the ticket to its next phase
    and re-dispatch its walker; the final gate completes the ticket.

    Args:
        ticket_id (`str`):
            The ticket waiting at its gate.
        body (`ApproveTicketRequest`):
            The human's note, logged on the gated decision.
        user_id (`str`):
            Injected authenticated user id.
        engine (`FactoryEngine`):
            Injected factory engine.

    Returns:
        `FactoryTicketDetailResponse`:
            The ticket's content as the approval left it.

    Raises:
        `HTTPException`:
            404 when the caller owns no such ticket; 409 when it is
            not WAITING_HUMAN (only a gate is answerable) or the gate
            was answered before this answer applied.
    """
    record = await engine.approve(user_id, ticket_id, body.note)
    return FactoryTicketDetailResponse(
        data=FactoryTicketView.from_record(record),
    )


@factory_router.post(
    "/{ticket_id}/reject",
    response_model=FactoryTicketDetailResponse,
    summary="Reject a gate",
)
async def reject_factory_ticket(
    ticket_id: str,
    body: RejectTicketRequest,
    user_id: str = Depends(get_current_user_id),
    engine: FactoryEngine = Depends(get_factory_engine),
) -> FactoryTicketDetailResponse:
    """Answer a gate with rejection: re-run the current phase.

    The phase starts over from scratch — a fresh discussion replaces
    the phase's recorded one — with the rejection and its note logged.

    Args:
        ticket_id (`str`):
            The ticket waiting at its gate.
        body (`RejectTicketRequest`):
            Why the phase re-runs, logged with the rejection.
        user_id (`str`):
            Injected authenticated user id.
        engine (`FactoryEngine`):
            Injected factory engine.

    Returns:
        `FactoryTicketDetailResponse`:
            The ticket's content as the rejection left it.

    Raises:
        `HTTPException`:
            404 when the caller owns no such ticket; 409 when it is
            not WAITING_HUMAN or the gate was already answered.
    """
    record = await engine.reject(user_id, ticket_id, body.note)
    return FactoryTicketDetailResponse(
        data=FactoryTicketView.from_record(record),
    )


@factory_router.post(
    "/{ticket_id}/cancel",
    response_model=FactoryTicketDetailResponse,
    summary="Cancel a ticket",
)
async def cancel_factory_ticket(
    ticket_id: str,
    user_id: str = Depends(get_current_user_id),
    engine: FactoryEngine = Depends(get_factory_engine),
) -> FactoryTicketDetailResponse:
    """Cancel a ticket: stop its walker, cascade into its discussion.

    Args:
        ticket_id (`str`):
            The ticket to cancel.
        user_id (`str`):
            Injected authenticated user id.
        engine (`FactoryEngine`):
            Injected factory engine.

    Returns:
        `FactoryTicketDetailResponse`:
            The cancelled ticket's content.

    Raises:
        `HTTPException`:
            404 when the caller owns no such ticket.
    """
    record = await engine.cancel(user_id, ticket_id)
    return FactoryTicketDetailResponse(
        data=FactoryTicketView.from_record(record),
    )


# The detail route stays last: fixed segments precede ``/{ticket_id}``
# in FastAPI's order-dependent path matching (the discussion router's
# discipline), and every earlier route here is a fixed segment itself.
@factory_router.get(
    "/{ticket_id}",
    response_model=FactoryTicketDetailResponse,
    summary="Read one ticket",
)
async def get_factory_ticket(
    ticket_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> FactoryTicketDetailResponse:
    """Return one ticket's persisted state.

    Args:
        ticket_id (`str`):
            The ticket to read.
        user_id (`str`):
            Injected authenticated user id; also scopes the lookup,
            so a caller only reads their own ticket.
        storage (`StorageBase`):
            Injected application storage.

    Returns:
        `FactoryTicketDetailResponse`:
            The ticket's content.

    Raises:
        `HTTPException`:
            404 when the caller owns no such ticket.
    """
    record = await storage.get_factory_ticket(user_id, ticket_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Factory ticket {ticket_id!r} not found.",
        )
    return FactoryTicketDetailResponse(
        data=FactoryTicketView.from_record(record),
    )
