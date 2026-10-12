# -*- coding: utf-8 -*-
"""Unit tests for the MS Teams push-notification channel."""

# pylint: disable=protected-access,missing-function-docstring
import asyncio
from typing import Any, AsyncIterator
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from agentscope.app.channel import MSTeamsChannel
from agentscope.app.channel._base import ChannelEvent, _NO_TEXT_REPLY
from agentscope.app.channel._registry import ChannelTypeRegistry
from agentscope.event import (
    DataBlockDeltaEvent,
    DataBlockEndEvent,
    DataBlockStartEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from agentscope.message import TextBlock, ToolCallBlock

_REPLY_ID = "reply-1"
_WEBHOOK_URL = "https://team.webhook.office.com/webhookb2/abc/def"
_MAX_TEXT = 16_384


class _FakeResponse:
    """A scripted webhook response."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeTeamsHTTP:
    """Recorded webhook posts and one scripted outcome for each."""

    def __init__(
        self,
        status_code: int = 200,
        body: str = "1",
        error: Exception | None = None,
    ) -> None:
        self._status_code = status_code
        self._body = body
        self._error = error
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.posts.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._status_code, self._body)

    async def aclose(self) -> None:
        self.closed = True


def _channel() -> MSTeamsChannel:
    """A channel bound to one Incoming Webhook URL."""
    return MSTeamsChannel(
        "teams-1",
        MSTeamsChannel.Credentials(webhook_url=_WEBHOOK_URL),
        MSTeamsChannel.Config(),
    )


def _message_event() -> ChannelEvent:
    """The synthetic event a reply targets (chat id is nominal)."""
    return ChannelEvent(
        channel_id="teams-1",
        channel_user_id="",
        chat_id="default",
        content=[TextBlock(text="hello")],
    )


def _cards(http: _FakeTeamsHTTP) -> list[dict[str, Any]]:
    """The posted MessageCards, in order."""
    return [kwargs["json"] for _, kwargs in http.posts]


async def _event_stream(text: str) -> AsyncIterator[dict]:
    """A run's events: one complete text reply."""
    events = [
        ReplyStartEvent(
            session_id="session-1",
            reply_id=_REPLY_ID,
            name="assistant",
        ),
        TextBlockStartEvent(reply_id=_REPLY_ID, block_id="text-1"),
        TextBlockDeltaEvent(
            reply_id=_REPLY_ID,
            block_id="text-1",
            delta=text,
        ),
        TextBlockEndEvent(reply_id=_REPLY_ID, block_id="text-1"),
        ReplyEndEvent(session_id="session-1", reply_id=_REPLY_ID),
    ]
    for event in events:
        yield event.model_dump(mode="json")


async def _data_block_stream() -> AsyncIterator[dict]:
    """A run's events: one text block followed by one image block."""
    events = [
        ReplyStartEvent(
            session_id="session-1",
            reply_id=_REPLY_ID,
            name="assistant",
        ),
        TextBlockStartEvent(reply_id=_REPLY_ID, block_id="text-1"),
        TextBlockDeltaEvent(
            reply_id=_REPLY_ID,
            block_id="text-1",
            delta="here is the chart",
        ),
        TextBlockEndEvent(reply_id=_REPLY_ID, block_id="text-1"),
        DataBlockStartEvent(
            reply_id=_REPLY_ID,
            block_id="image-1",
            media_type="image/png",
            name="chart.png",
        ),
        DataBlockDeltaEvent(
            reply_id=_REPLY_ID,
            block_id="image-1",
            media_type="image/png",
            data="aW1n",
        ),
        DataBlockEndEvent(reply_id=_REPLY_ID, block_id="image-1"),
        ReplyEndEvent(session_id="session-1", reply_id=_REPLY_ID),
    ]
    for event in events:
        yield event.model_dump(mode="json")


async def _confirmation_stream() -> AsyncIterator[dict]:
    """A run's events: parked awaiting the user's approval."""
    events = [
        ReplyStartEvent(
            session_id="session-1",
            reply_id=_REPLY_ID,
            name="assistant",
        ),
        RequireUserConfirmEvent(
            reply_id=_REPLY_ID,
            tool_calls=[
                ToolCallBlock(
                    id="tool-1",
                    name="Bash",
                    input='{"command":"ls"}',
                ),
            ],
        ),
    ]
    for event in events:
        yield event.model_dump(mode="json")


async def _send(
    channel: MSTeamsChannel,
    http: _FakeTeamsHTTP,
    stream: AsyncIterator[dict],
) -> None:
    """Deliver one run's events with the fake client injected."""
    with patch.object(channel, "_new_http_client", return_value=http):
        await channel.send_response(_message_event(), stream)
        await channel.aclose()


async def _notify(
    channel: MSTeamsChannel,
    http: _FakeTeamsHTTP,
    title: str | None,
    text: str,
) -> None:
    """Send one notification with the fake client injected."""
    with patch.object(channel, "_new_http_client", return_value=http):
        await channel.send_notification(title, text)
        await channel.aclose()


class MSTeamsChannelIdentityTest(IsolatedAsyncioTestCase):
    """Type metadata and registry integration."""

    async def test_identity_and_registry_schema(self) -> None:
        channel = _channel()

        self.assertEqual(channel.channel_id, "teams-1")
        self.assertEqual(MSTeamsChannel.channel_type, "msteams")

        registry = ChannelTypeRegistry([MSTeamsChannel])
        schema = registry.list_types()[0]
        self.assertEqual(schema.channel_type, "msteams")
        self.assertEqual(schema.platform_bot_id_field, "webhook_url")
        secret = schema.credentials_schema["properties"]["webhook_url"]
        self.assertEqual(secret["format"], "password")
        # An empty model still yields a (property-less) form schema.
        self.assertEqual(schema.config_schema["properties"], {})
        # The webhook URL doubles as the bot identity for uniqueness.
        self.assertEqual(
            registry.extract_platform_bot_id(
                "msteams",
                {"webhook_url": _WEBHOOK_URL},
            ),
            _WEBHOOK_URL,
        )
        self.assertEqual(
            MSTeamsChannel.capabilities.max_message_length,
            _MAX_TEXT,
        )


class MSTeamsListenerTest(IsolatedAsyncioTestCase):
    """The idle (one-way webhook) listener lifecycle."""

    async def test_idle_listener_reports_connected_until_cancelled(
        self,
    ) -> None:
        channel = _channel()

        async def emit(event: object) -> None:
            pass

        task = asyncio.create_task(channel.start_listening(emit))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2.0
        while channel.status.state != "connected" and loop.time() < deadline:
            await asyncio.sleep(0.01)

        self.assertEqual(channel.status.state, "connected")

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(channel.status.state, "stopped")


class MSTeamsSendNotificationTest(IsolatedAsyncioTestCase):
    """MessageCard delivery to the webhook."""

    async def test_post_is_a_message_card(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        await _notify(channel, http, "Build failed", "deploy 42 broke")

        self.assertEqual(len(http.posts), 1)
        url, kwargs = http.posts[0]
        self.assertEqual(url, _WEBHOOK_URL)
        # The URL is itself the credential — no auth header is sent.
        self.assertNotIn("headers", kwargs)
        card = kwargs["json"]
        self.assertEqual(card["@type"], "MessageCard")
        self.assertEqual(
            card["@context"],
            "https://schema.org/extensions",
        )
        self.assertEqual(card["title"], "Build failed")
        self.assertEqual(card["text"], "deploy 42 broke")
        self.assertEqual(card["themeColor"], "0076D7")
        self.assertTrue(http.closed)

    async def test_untitled_notification_omits_the_title(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        await _notify(channel, http, None, "plain note")

        card = _cards(http)[0]
        self.assertNotIn("title", card)
        self.assertEqual(card["text"], "plain note")

    async def test_non_2xx_raises_with_status_and_body(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP(
            status_code=400,
            body="Bad Request " + "x" * 400,
        )

        with self.assertRaises(RuntimeError) as ctx:
            await _notify(channel, http, None, "hi")

        message = str(ctx.exception)
        self.assertIn("Teams webhook rejected", message)
        self.assertIn("400", message)
        self.assertIn("Bad Request", message)
        # The echoed body is truncated so the error stays readable.
        self.assertLessEqual(len(message.split("400 ", 1)[1]), 200)

    async def test_network_error_raises(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP(error=RuntimeError("dns blew up"))

        with self.assertRaises(RuntimeError) as ctx:
            await _notify(channel, http, None, "hi")

        self.assertIn("dns blew up", str(ctx.exception))


class MSTeamsSendResponseTest(IsolatedAsyncioTestCase):
    """Reply accumulation and card rendering."""

    async def test_reply_is_sent_as_one_untitled_card(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        await _send(channel, http, _event_stream("hello from agent"))

        self.assertEqual(len(http.posts), 1)
        card = _cards(http)[0]
        self.assertNotIn("title", card)
        self.assertEqual(card["text"], "hello from agent")
        self.assertTrue(http.closed)

    async def test_data_block_is_skipped_and_logged(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        with self.assertLogs("as", level="DEBUG") as captured:
            await _send(channel, http, _data_block_stream())

        # Only the text block became a card; the image was dropped.
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(_cards(http)[0]["text"], "here is the chart")
        self.assertIn(
            "cannot deliver a data block",
            "\n".join(captured.output),
        )

    async def test_confirmation_is_a_titled_shield_card(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        await _send(channel, http, _confirmation_stream())

        # No text accumulated, so the base's placeholder went out first
        # (same as the other channels), then the approval card.
        self.assertEqual(len(http.posts), 2)
        first = _cards(http)[0]
        self.assertEqual(first["text"], _NO_TEXT_REPLY)
        second = _cards(http)[1]
        self.assertEqual(second["title"], "Bash")
        self.assertIn("🛡️", second["text"])
        self.assertIn("approval", second["text"])
        self.assertIn('{"command":"ls"}', second["text"])

    async def test_empty_reply_sends_the_no_text_placeholder(
        self,
    ) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        await _send(
            channel,
            http,
            _event_stream(""),
        )

        self.assertEqual(_cards(http)[0]["text"], _NO_TEXT_REPLY)


class MSTeamsTruncationTest(IsolatedAsyncioTestCase):
    """The MessageCard text cap."""

    async def test_over_limit_text_is_truncated_with_marker(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        await _notify(channel, http, None, "x" * 20_000)

        text = _cards(http)[0]["text"]
        self.assertEqual(len(text), _MAX_TEXT)
        self.assertTrue(text.startswith("x" * 100))
        self.assertTrue(text.endswith("… (truncated)"))

    async def test_at_limit_text_is_sent_whole(self) -> None:
        channel = _channel()
        http = _FakeTeamsHTTP()

        await _notify(channel, http, None, "y" * _MAX_TEXT)

        self.assertEqual(_cards(http)[0]["text"], "y" * _MAX_TEXT)
