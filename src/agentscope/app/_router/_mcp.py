# -*- coding: utf-8 -*-
"""MCP router — the user's own library of installed MCPs.

This is the user-level collection an install lands in, distinct from
``/workspace/mcp``, which manages the MCPs of one session's workspace.
Installing from a hub or creating from a preset only writes here;
putting an MCP into a workspace stays a separate, explicit act.
"""
import asyncio

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
    UpdateMCPRequest,
)

mcp_router = APIRouter(prefix="/mcp", tags=["mcp"])

# The reserved hub id marking a record born from the preset catalog —
# its "card" is the catalog entry named by ``card_id``, so PATCH can
# re-render without any hub being registered.
PRESET_HUB_ID = "preset"

# The per-server budget for listing tools. One unreachable server must
# not hold the whole listing hostage; see ``_tool_group``.
TOOL_LIST_TIMEOUT = 15.0


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


async def _tool_group(record: MCPRecord) -> McpToolGroupView:
    """List one installed MCP's tools, or none of them on any failure.

    A server that is down, slow, or misbehaving — a stateful one with
    no live session included — simply contributes an empty group: this
    listing is advisory, for picking tool names, so it carries no error
    field and a broken server stays silent rather than loud.

    Args:
        record (`MCPRecord`):
            The installed-MCP record to list from.

    Returns:
        `McpToolGroupView`:
            The group for this MCP, its tools possibly empty.
    """
    try:
        tools = await asyncio.wait_for(
            record.client.list_tools(),
            timeout=TOOL_LIST_TIMEOUT,
        )
    except Exception:
        tools = []
    return McpToolGroupView(
        name=record.client.name,
        tools=[
            McpToolView(name=tool.name, description=tool.description)
            for tool in tools
        ],
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
    exists. Groups run concurrently, one per enabled MCP in library
    order; unhealthy servers contribute an empty group, and the total
    counts tools, not servers.
    """
    records = [r for r in await storage.list_mcps(user_id) if r.enabled]
    groups = await asyncio.gather(*(_tool_group(r) for r in records))
    return ListMcpToolsResponse(
        groups=list(groups),
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
