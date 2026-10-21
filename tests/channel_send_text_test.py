# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The unified ``send_text`` contract: workflow notifications.

Each platform resolves its own default target — there is no inbound
event to address the reply to — and raises when the send fails, so the
workflow notify node can surface the failure and retry it. These tests
pin the target resolution and the text passthrough per channel type
with the platform call faked; the platform calls themselves are
covered by each channel's own tests.
"""
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from agentscope.app.channel import (
    ChannelBase,
    ChannelEvent,
    ChannelStatus,
    DiscordChannel,
    FeishuChannel,
    MSTeamsChannel,
)


def _teams() -> MSTeamsChannel:
    """A Teams channel pointed at a fake webhook."""
    return MSTeamsChannel(
        "mt-1",
        MSTeamsChannel.Credentials(webhook_url="https://hook"),
        MSTeamsChannel.Config(),
    )


def _feishu(**config: Any) -> FeishuChannel:
    """A Feishu channel with the given platform options."""
    return FeishuChannel(
        "fs-1",
        FeishuChannel.Credentials(app_id="app", app_secret="secret"),
        FeishuChannel.Config(**config),
    )


def _discord() -> DiscordChannel:
    """A Discord channel; no SDK import happens at construction."""
    return DiscordChannel(
        "dc-1",
        DiscordChannel.Credentials(
            bot_token="token",
            application_id="app",
        ),
        DiscordChannel.Config(),
    )


class _UnsupportedChannel(ChannelBase):
    """A minimal channel that implements nothing beyond the ABC."""

    channel_type = "unsupported"
    display_name = "Unsupported"
    platform_bot_id_field = "bot_id"

    def __init__(self) -> None:
        self.status = ChannelStatus()

    @property
    def channel_id(self) -> str:
        """The fixed test id."""
        return "un-1"

    async def start_listening(self, emit: Any) -> None:
        """Never called."""

    async def send_response(
        self,
        event: ChannelEvent,
        events: Any,
    ) -> None:
        """Never called."""


class SendTextContractTest(IsolatedAsyncioTestCase):
    """Default target resolution and failure signalling per type."""

    async def test_the_base_default_names_the_gap(self) -> None:
        """A channel type that did not implement send_text fails with
        an error that names workflow notifications as the gap."""
        with self.assertRaises(NotImplementedError) as ctx:
            await _UnsupportedChannel().send_text("hi")
        self.assertIn("workflow notifications", str(ctx.exception))

    async def test_discord_keeps_the_default(self) -> None:
        """Discord cannot send without a live conversation object, so
        v1 leaves the base default in place."""
        with self.assertRaises(NotImplementedError):
            await _discord().send_text("hi")

    async def test_msteams_sends_an_untitled_card(self) -> None:
        """The Incoming Webhook is itself the default target, so the
        notification is the plain send with no title."""
        channel = _teams()
        sent: list[tuple[str | None, str]] = []

        async def fake_notification(
            title: str | None,
            text: str,
        ) -> None:
            sent.append((title, text))

        with patch.object(channel, "send_notification", fake_notification):
            await channel.send_text("deploy done")

        self.assertEqual(sent, [(None, "deploy done")])

    async def test_feishu_uses_the_configured_default_target(self) -> None:
        channel = _feishu(
            default_receive_id="oc_chat",
            default_receive_id_type="chat_id",
        )
        sent: list[tuple[str, str, str]] = []

        async def fake_send_message_to(
            receive_id: str,
            receive_id_type: str,
            text: str,
        ) -> dict:
            sent.append((receive_id, receive_id_type, text))
            return {"code": 0}

        with patch.object(channel, "send_message_to", fake_send_message_to):
            await channel.send_text("deploy done")

        self.assertEqual(sent, [("oc_chat", "chat_id", "deploy done")])

    async def test_feishu_without_a_default_target_raises(self) -> None:
        channel = _feishu()
        with self.assertRaises(ValueError) as ctx:
            await channel.send_text("hi")
        self.assertIn("default receive id", str(ctx.exception))

    async def test_feishu_reports_a_rejected_send(self) -> None:
        """A None API answer must raise, not read as delivered — the
        notify node counts on the failure to retry."""
        channel = _feishu(default_receive_id="oc_chat")

        async def rejected(
            receive_id: str,  # pylint: disable=unused-argument
            receive_id_type: str,  # pylint: disable=unused-argument
            text: str,  # pylint: disable=unused-argument
        ) -> None:
            return None

        with patch.object(channel, "send_message_to", rejected):
            with self.assertRaises(RuntimeError) as ctx:
                await channel.send_text("hi")
        self.assertIn("oc_chat", str(ctx.exception))

    async def test_feishu_reports_a_rejection_body(self) -> None:
        """A rejected send answers a non-None body with a non-zero
        code (bot removed from the chat, typo'd receive id, revoked
        permission) — the exact shape the API helper hands back after
        logging it. Reading it as delivered would silence precisely
        the failure class the notify node exists to surface."""
        channel = _feishu(default_receive_id="oc_chat")

        async def rejected(
            receive_id: str,  # pylint: disable=unused-argument
            receive_id_type: str,  # pylint: disable=unused-argument
            text: str,  # pylint: disable=unused-argument
        ) -> dict:
            return {"code": 230002, "msg": "receive_id does not exist"}

        with patch.object(channel, "send_message_to", rejected):
            with self.assertRaises(RuntimeError) as ctx:
                await channel.send_text("hi")

        self.assertIn("receive_id does not exist", str(ctx.exception))
