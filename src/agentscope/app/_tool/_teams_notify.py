# -*- coding: utf-8 -*-
"""The MS Teams notification tool: push a card from a chat turn.

Finds the calling user's MS Teams channel record in storage and POSTs
one card through a throwaway sender-side :class:`MSTeamsChannel` built
from that record — the same nested Credentials / Config validation the
channel registry performs, without the registry itself (the tool binds
no message bus, so the heavier :class:`ChannelClients` cache would buy
nothing for a one-shot send). The channel's lazily opened HTTP client
is closed again on the way out.
"""
from typing import Any

from pydantic import BaseModel, Field

from ...message import TextBlock, ToolResultState
from ...permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ...tool import ToolBase, ToolChunk
from ..channel import MSTeamsChannel
from ..storage import StorageBase


class _TeamsNotifyParams(BaseModel):
    """The params for the send_teams_notification tool."""

    title: str = Field(
        default="",
        description="Card title; empty sends the card untitled.",
    )

    text: str = Field(
        description="Notification body (Markdown-ish plain text).",
    )


class TeamsNotify(ToolBase):
    """The send_teams_notification tool.

    Sends one push notification to the calling user's MS Teams
    channel. The channel is looked up per call — the first msteams
    record the user owns wins, with a note when several exist — so a
    channel created mid-conversation works on the next call without a
    toolkit refresh. Everything the lookup misses or the webhook
    refuses comes back as an error chunk; the tool never raises.
    """

    name: str = "send_teams_notification"

    description: str = (
        "Send a push notification to the user's MS Teams channel. The "
        "notification appears as a card in the Teams channel connected "
        "through an Incoming Webhook."
    )

    input_schema: dict = _TeamsNotifyParams.model_json_schema()

    is_concurrency_safe: bool = True
    is_read_only: bool = False
    is_state_injected: bool = False
    is_external_tool: bool = False
    is_mcp: bool = False
    mcp_name: str | None = None

    def __init__(self, user_id: str, storage: StorageBase) -> None:
        """Bind the tool to one user's storage.

        Args:
            user_id (`str`): The user whose MS Teams channel the
                notification goes to.
            storage (`StorageBase`): Application storage, where the
                channel records live.
        """
        super().__init__()
        self._user_id = user_id
        self._storage = storage

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Check permission for the tool usage."""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed to be called.",
        )

    async def call(self, title: str = "", text: str = "") -> ToolChunk:
        """Look up the channel and deliver the notification.

        Args:
            title (`str`, optional): Card title; empty sends the card
                untitled.
            text (`str`): Notification body.

        Returns:
            `ToolChunk`: A delivery confirmation, or an error
                description. Never raises.
        """
        try:
            return await self._notify(title, text)
        except Exception as exc:  # pylint: disable=broad-except
            return _error_chunk(f"send_teams_notification failed: {exc}")

    async def _notify(self, title: str, text: str) -> ToolChunk:
        """Resolve the channel, POST the card, and close the client."""
        records = await self._storage.list_channels(self._user_id)
        teams = [
            record
            for record in records
            if record.channel_type == MSTeamsChannel.channel_type
        ]
        if not teams:
            return _error_chunk(
                "No MS Teams channel is configured. Create one on the "
                "Channels page first.",
            )
        record = teams[0]
        channel = MSTeamsChannel(
            record.id,
            MSTeamsChannel.Credentials(**record.credentials),
            MSTeamsChannel.Config(**record.platform_config),
        )
        try:
            await channel.send_notification(title or None, text)
        finally:
            await channel.aclose()
        note = ""
        if len(teams) > 1:
            note = (
                f" ({len(teams)} MS Teams channels are configured; the "
                f"first, {record.name or record.id}, was used.)"
            )
        return ToolChunk(
            content=[
                TextBlock(
                    text=f"Notification delivered to MS Teams.{note}",
                ),
            ],
            state=ToolResultState.SUCCESS,
        )


def _error_chunk(text: str) -> ToolChunk:
    """An error-state chunk carrying *text*."""
    return ToolChunk(
        content=[TextBlock(text=text)],
        state=ToolResultState.ERROR,
    )
