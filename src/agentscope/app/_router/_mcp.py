# -*- coding: utf-8 -*-
"""MCP router — the user's own library of installed MCPs.

This is the user-level collection an install lands in, distinct from
``/workspace/mcp``, which manages the MCPs of one session's workspace.
Installing from a hub or creating from a preset only writes here;
putting an MCP into a workspace stays a separate, explicit act.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError

from ..deps import get_current_user_id, get_mcp_hubs, get_storage
from ..hub import MCPHubBase
from .._service import (
    MCPRenderError,
    PRESETS,
    get_preset,
    preset_view,
    render_mcp,
    render_preset,
)
from ..storage import MCPRecord, StorageBase
from ...mcp import MCPClient
from ._schema import (
    CreateFromPresetRequest,
    CreateFromPresetResponse,
    ListMcpToolsResponse,
    ListPresetsResponse,
    MCPView,
    McpToolGroupView,
    McpToolView,
    PresetView,
    TestMcpResult,
    UpdateMCPRequest,
)

mcp_router = APIRouter(prefix="/mcp", tags=["mcp"])

# The reserved hub id marking a record born from the preset catalog —
# its "card" is the catalog entry named by ``card_id``, so PATCH can
# re-render without any hub being registered.
PRESET_HUB_ID = "preset"

# The per-server budget for listing tools. One unreachable server must
# not hold the whole listing hostage; see ``_tool_group``. Generous on
# purpose: a stateful stdio server's first start (npx downloading its
# package, uvx resolving) can easily exceed 15s on Windows.
TOOL_LIST_TIMEOUT = 45.0

logger = logging.getLogger(__name__)


@mcp_router.get("")
async def list_mcps(
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> list[MCPView]:
    """Return every MCP the user has installed, ordered by name."""
    records = await storage.list_mcps(user_id)
    return sorted(
        (MCPView.from_record(r) for r in records),
        key=lambda view: view.name,
    )


# Declared before the ``/{mcp_id}`` routes: a literal path segment must
# not risk being captured as someone's record id.
@mcp_router.get("/presets")
async def list_presets() -> ListPresetsResponse:
    """List the built-in integration presets.

    Static catalog data with nothing per-user in it, so it takes no
    storage and no hub — it is the no-hub path into this library.
    """
    return ListPresetsResponse(
        presets=[PresetView(**preset_view(p)) for p in PRESETS],
        total=len(PRESETS),
    )


async def _connected_tools(client: MCPClient) -> list:
    """List one client's tools, connecting first when it is stateful.

    A stateful client needs a live session before it can answer, so the
    connection is opened here and always closed again — the caller owns
    only a throwaway copy, never the stored record, so nothing leaks
    and the library keeps no connection state.

    Args:
        client (`MCPClient`):
            The client to list from.

    Returns:
        `list`: The client's tools.
    """
    if not client.is_stateful:
        return await client.list_tools()
    await client.connect()
    try:
        return await client.list_tools()
    finally:
        await client.close()


async def _tool_group(record: MCPRecord) -> McpToolGroupView:
    """List one installed MCP's tools, or none of them on any failure.

    A stateful record — the default for preset and hub installs — is
    listed through a throwaway copy of its client, connected and closed
    inside the budget, mirroring how a workspace starts one without
    touching the stored record. A server that is down, slow, or
    misbehaving contributes a group with empty ``tools`` and an
    ``error`` saying why: this listing is advisory, for picking tool
    names, so one broken server never sinks the others — but it stays
    visible in the picker with its reason rather than vanishing.

    Args:
        record (`MCPRecord`):
            The installed-MCP record to list from.

    Returns:
        `McpToolGroupView`:
            The group for this MCP, its tools possibly empty.
    """
    # A copy, not the stored instance: connecting mutates connection
    # state, and the record is the library's desired state, not a live
    # session (a workspace copies for the same reason).
    client = MCPClient.model_validate(record.client.model_dump(mode="json"))
    error: str | None = None
    try:
        tools = await asyncio.wait_for(
            _connected_tools(client),
            timeout=TOOL_LIST_TIMEOUT,
        )
    except Exception as exc:
        # Loud in the log so "why are my tools not listed" is answerable
        # from the console, and now also surfaced on the group itself:
        # an enabled MCP whose server could not be reached still shows
        # up in the picker — with this reason — instead of vanishing and
        # leaving the user no tools to choose from. The message is kept
        # short and human; the full repr stays in the log.
        logger.warning(
            "Tool listing for MCP %r failed (group left empty): %r",
            record.client.name,
            exc,
        )
        tools = []
        if isinstance(exc, asyncio.TimeoutError):
            error = (
                "Timed out listing tools — the server did not respond "
                f"within {int(TOOL_LIST_TIMEOUT)}s."
            )
        else:
            reason = str(exc).strip() or exc.__class__.__name__
            error = f"Could not list tools: {reason}"
    return McpToolGroupView(
        name=record.client.name,
        tools=[
            McpToolView(name=tool.name, description=tool.description)
            for tool in tools
        ],
        error=error,
    )


# Declared before the ``/{mcp_id}`` routes, like ``/presets`` above: a
# literal path segment must not risk being captured as a record id.
@mcp_router.get("/tools")
async def list_mcp_tools(
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> ListMcpToolsResponse:
    """List the tools the user's enabled MCPs make available.

    Distinct from ``GET /workspace/mcp``: that one reports the live
    clients of one running session, while this works from the library
    alone, so the agent form can offer tool names before any session
    exists. Groups run concurrently, one per enabled MCP, sorted by MCP
    name; an unhealthy server contributes a group with empty ``tools``
    and an ``error`` saying why, and the total counts tools, not
    servers.
    """
    records = [r for r in await storage.list_mcps(user_id) if r.enabled]
    groups = await asyncio.gather(*(_tool_group(r) for r in records))
    return ListMcpToolsResponse(
        groups=sorted(groups, key=lambda group: group.name),
        total=sum(len(group.tools) for group in groups),
    )


@mcp_router.post("", status_code=status.HTTP_201_CREATED)
async def create_mcp(
    body: CreateFromPresetRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> CreateFromPresetResponse:
    """Fill a preset's fields and add the result to the library.

    Mirrors a hub install, minus the hub: the values become environment
    variables on the preset's fixed command, and the record keeps
    ``(hub_id="preset", card_id=preset_id)`` so a later re-key can
    re-render from the catalog entry instead of a card. Like an
    install, the config is not connection-tested here.

    Raises:
        `HTTPException`: 404 if the preset is not in the catalog, 422
            if a required field is missing or the name is not made of
            ``[a-zA-Z0-9_-]``, or 409 if the name is taken.
    """
    preset = get_preset(body.preset_id)
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No integration preset with id {body.preset_id!r}.",
        )

    # Every catalog field is required, modulo the defaults the catalog
    # itself supplies; the answer set is completed before validation so
    # a field with a default is never reported as missing.
    values = {f.name: f.default for f in preset.fields if f.default}
    # Reject unknown keys so the stored answer set stays honest — an
    # extra key would otherwise persist forever, silently unused.
    unknown = [
        key
        for key in body.values
        if key not in {f.name for f in preset.fields}
    ]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Preset {preset.id!r} has no field(s): "
                f"{', '.join(sorted(unknown))}."
            ),
        )
    values.update(body.values)
    missing = [f.name for f in preset.fields if not values.get(f.name)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Preset {preset.id!r} needs a value for: "
                f"{', '.join(missing)}."
            ),
        )

    try:
        client = render_preset(preset, values, body.name)
    except ValueError as e:
        # ``MCPClient`` refusing the name — the same check PATCH makes.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        ) from e

    record = MCPRecord(
        user_id=user_id,
        client=client,
        values=values,
        display_name=preset.name,
        description=preset.description,
        hub_id=PRESET_HUB_ID,
        card_id=preset.id,
    )
    try:
        mcp_id = await storage.upsert_mcp(user_id, record)
    except ValueError as e:
        # The name is unique per user, so a clash is the caller's to
        # resolve by passing a different one.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{e} Pass a different 'name' to add it alongside.",
        ) from e

    return CreateFromPresetResponse(mcp_id=mcp_id)


@mcp_router.patch("/{mcp_id}")
async def update_mcp(
    mcp_id: str,
    body: UpdateMCPRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    hubs: dict[str, MCPHubBase] = Depends(get_mcp_hubs),
) -> MCPView:
    """Rename, enable/disable, or re-key an installed MCP.

    Changing a secret is a re-render, not a field edit: an API key can
    sit anywhere in the config — inside a URL, a header, an env var — so
    the card's template plus the merged answers is the only thing that
    knows where to put it. Preset records re-render from the catalog
    entry the same way; hand-added records have neither, and are
    refused.

    Raises:
        `HTTPException`: 404 if the MCP is not in the caller's library,
            400 if the MCP has no card or preset to re-render from, 409
            if the new name is taken, or 422 if the name is not made of
            ``[a-zA-Z0-9_-]``.
    """
    record = await storage.get_mcp(user_id, mcp_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No installed MCP with id {mcp_id!r}.",
        )

    if body.values is not None:
        if record.hub_id == PRESET_HUB_ID:
            # A preset record re-renders from the catalog entry, not a
            # hub card; the reserved hub id keeps it out of the hub
            # lookup below even when one shares the name.
            preset = get_preset(record.card_id or "")
            if preset is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "This MCP has no preset to re-render from — the "
                        "catalog no longer has the entry it was created "
                        "from."
                    ),
                )
            # Merged, not replaced, mirroring the hub flow: the client
            # sends only what changed, and a secret the form did not
            # echo back must survive. Unknown keys are rejected so they
            # cannot linger in the stored answer set, silently unused.
            unknown = [
                key
                for key in body.values
                if key not in {f.name for f in preset.fields}
            ]
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"Preset {preset.id!r} has no field(s): "
                        f"{', '.join(sorted(unknown))}."
                    ),
                )
            merged = {**record.values, **body.values}
            missing = [
                f.name for f in preset.fields if not merged.get(f.name)
            ]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"Preset {preset.id!r} needs a value for: "
                        f"{', '.join(missing)}."
                    ),
                )
            try:
                record.client = render_preset(
                    preset,
                    merged,
                    body.name or record.client.name,
                )
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(e),
                ) from e
            record.values = merged
        else:
            hub = hubs.get(record.hub_id or "")
            if hub is None or record.card_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "This MCP has no card to re-render from — its "
                        "hub is not registered, or it was added by hand."
                    ),
                )
            try:
                card = await hub.get_mcp(user_id, record.card_id)
            except KeyError as e:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"Hub {record.hub_id!r} no longer has the card "
                        f"this MCP was installed from."
                    ),
                ) from e

            # Merged, not replaced: the client sends only what changed,
            # and a write-only field the form did not echo back must
            # survive.
            merged = {**record.values, **body.values}
            try:
                record.client = render_mcp(
                    card,
                    merged,
                    body.name or record.client.name,
                )
            except MCPRenderError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                ) from e
            record.values = merged
            record.version = card.version
    elif body.name is not None:
        # ``MCPClient`` checks its name in ``model_post_init``, which a
        # plain attribute assignment skips; rebuild the client through
        # ``model_validate`` so an illegal name is refused here. Stored
        # as-is it would fail the same check on the next read, turning
        # every route that loads the library into a 500.
        try:
            record.client = MCPClient.model_validate(
                {**record.client.model_dump(), "name": body.name},
            )
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    if body.enabled is not None:
        record.enabled = body.enabled

    try:
        await storage.upsert_mcp(user_id, record)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        ) from e

    return MCPView.from_record(record)


@mcp_router.delete("/{mcp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp(
    mcp_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> None:
    """Remove an MCP from the user's library.

    Workspaces that already hold this MCP keep it — the workspace
    relation is derived by name, not a foreign key, and tearing down a
    live stateful MCP session as a side effect of a library edit would
    surprise the user.
    """
    if not await storage.delete_mcp(user_id, mcp_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No installed MCP with id {mcp_id!r}.",
        )


@mcp_router.post("/{mcp_id}/test")
async def test_mcp(
    mcp_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> TestMcpResult:
    """Check that one installed MCP actually connects and answers.

    Unlike install and re-key — which never dial the server — this is
    the explicit "does my integration work" button: it walks a
    throwaway copy of the record's client through connect/list/close
    (the same path ``/mcp/tools`` uses per group) within the per-server
    budget, so the stored record is never mutated and no connection
    leaks. A wrong API key, an unreachable host, or a server that dies
    at startup is reported as ``ok=False`` with the reason in
    ``detail`` — a 200 with failure data, like the credential test, not
    a thrown error — so the UI can show it inline. On success ``detail``
    names the tool count and ``tool_count`` carries it.

    Raises:
        `HTTPException`: 404 if the MCP is not in the caller's library.
    """
    record = await storage.get_mcp(user_id, mcp_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No installed MCP with id {mcp_id!r}.",
        )

    # A copy, not the stored instance: connecting mutates connection
    # state, and the record is desired state, not a live session — the
    # same reason ``_tool_group`` copies before listing.
    client = MCPClient.model_validate(record.client.model_dump(mode="json"))
    try:
        tools = await asyncio.wait_for(
            _connected_tools(client),
            timeout=TOOL_LIST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Connection test for MCP %r timed out.",
            record.client.name,
        )
        return TestMcpResult(
            ok=False,
            detail=(
                "Timed out connecting — the server did not respond "
                f"within {int(TOOL_LIST_TIMEOUT)}s."
            ),
        )
    except Exception as exc:
        logger.warning(
            "Connection test for MCP %r failed: %r",
            record.client.name,
            exc,
        )
        reason = str(exc).strip() or exc.__class__.__name__
        return TestMcpResult(
            ok=False,
            detail=f"Could not connect: {reason}",
        )

    count = len(tools)
    return TestMcpResult(
        ok=True,
        detail=f"Connected successfully — {count} tool(s) available.",
        tool_count=count,
    )
