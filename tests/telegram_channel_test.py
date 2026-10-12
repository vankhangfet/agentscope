# -*- coding: utf-8 -*-
"""Unit tests for the Telegram two-way bot channel."""

# pylint: disable=protected-access,missing-function-docstring
import asyncio
from typing import Any, AsyncIterator, Callable
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from agentscope.app.channel import TelegramChannel
from agentscope.app.channel._base import ChannelEvent, _NO_TEXT_REPLY
from agentscope.app.channel._registry import ChannelTypeRegistry
from agentscope.app.channel._telegram import _channel as telegram_module
from agentscope.event import (
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from agentscope.message import TextBlock, ToolCallBlock

_REPLY_ID = "reply-1"


class _FakeResponse:
    """A scripted Telegram API response."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeTelegramHTTP:
    """Scripted getUpdates pages and recorded sendMessage calls.

    Pages are popped in order; once exhausted, ``get`` parks forever —
    the stand-in for the server holding an empty long poll for ~25s —
    so the listener under test behaves like production and cancellation
    lands mid-poll.
    """

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        failures: int = 0,
    ) -> None:
        self._pages = list(pages or [])
        self._failures = failures
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.on_get: Callable[[], None] | None = None

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.gets.append((url, kwargs))
        if self.on_get is not None:
            self.on_get()
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("telegram is down")
        if self._pages:
            return _FakeResponse(self._pages.pop(0))
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.posts.append((url, kwargs))
        return _FakeResponse({"ok": True, "result": {"message_id": 1}})

    async def aclose(self) -> None:
        self.closed = True


def _page(*updates: dict[str, Any]) -> dict[str, Any]:
    """One getUpdates response page."""
    return {"ok": True, "result": list(updates)}


def _rejected_page() -> dict[str, Any]:
    """One getUpdates rejection body (e.g. a revoked token)."""
    return {
        "ok": False,
        "error_code": 401,
        "description": "Unauthorized",
    }


def _update(
    update_id: int,
    *,
    chat_id: int = 111,
    text: str | None = "hello",
    user_id: int = 42,
    first_name: str = "Alice",
) -> dict[str, Any]:
    """One update carrying a text message."""
    message: dict[str, Any] = {
        "message_id": update_id * 10,
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }
    if user_id is not None:
        message["from"] = {"id": user_id, "first_name": first_name}
    return {"update_id": update_id, "message": message}


def _channel(allowed: list[str] | None = None) -> TelegramChannel:
    """A channel with a token and an optional chat allow-list."""
    return TelegramChannel(
        "tg-1",
        TelegramChannel.Credentials(bot_token="token-1"),
        TelegramChannel.Config(allowed_chat_ids=allowed or []),
    )


def _message_event(chat_id: str = "111") -> ChannelEvent:
    """The inbound event a reply targets."""
    return ChannelEvent(
        channel_id="tg-1",
        channel_user_id="42",
        chat_id=chat_id,
        content=[TextBlock(text="hello")],
    )


async def _drive(
    channel: TelegramChannel,
    http: _FakeTelegramHTTP,
    until: Callable[[], bool],
    received: list[ChannelEvent],
) -> asyncio.Task:
    """Run the listener until ``until()`` holds; the caller cancels."""

    async def emit(event: ChannelEvent) -> None:
        received.append(event)

    async def run() -> None:
        with patch.object(channel, "_new_http_client", return_value=http):
            await channel.start_listening(emit)

    task = asyncio.create_task(run())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while not until() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return task


async def _event_stream(text: str = "hello from agent") -> AsyncIterator[dict]:
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
    channel: TelegramChannel,
    http: _FakeTelegramHTTP,
    stream: AsyncIterator[dict],
) -> None:
    """Deliver one run's events with the fake client injected."""
    with patch.object(channel, "_new_http_client", return_value=http):
        await channel.send_response(_message_event(), stream)
        await channel.aclose()


class TelegramChannelIdentityTest(IsolatedAsyncioTestCase):
    """Type metadata and registry integration."""

    async def test_identity_and_registry_schema(self) -> None:
        channel = _channel()

        self.assertEqual(channel.channel_id, "tg-1")
        self.assertEqual(TelegramChannel.channel_type, "telegram")

        registry = ChannelTypeRegistry([TelegramChannel])
        schema = registry.list_types()[0]
        self.assertEqual(schema.channel_type, "telegram")
        self.assertEqual(schema.platform_bot_id_field, "bot_token")
        secret = schema.credentials_schema["properties"]["bot_token"]
        self.assertEqual(secret["format"], "password")
        self.assertIn("allowed_chat_ids", schema.config_schema["properties"])
        self.assertEqual(
            TelegramChannel.capabilities.max_message_length,
            4096,
        )


class TelegramListenerTest(IsolatedAsyncioTestCase):
    """getUpdates polling, mapping, and lifecycle."""

    async def test_updates_map_to_events_with_offset_advancing(
        self,
    ) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP(
            pages=[
                _page(_update(1), _update(2, chat_id=222, text="second")),
                _page(_update(10, text="third")),
            ],
        )
        received: list[ChannelEvent] = []
        task = await _drive(
            channel,
            http,
            until=lambda: len(http.gets) >= 3,
            received=received,
        )

        self.assertEqual(
            [event.message for event in received],
            ["hello", "second", "third"],
        )
        first = received[0]
        self.assertEqual(first.channel_id, "tg-1")
        self.assertEqual(first.channel_user_id, "42")
        self.assertEqual(first.channel_user_name, "Alice")
        self.assertEqual(first.chat_id, "111")
        self.assertEqual(first.channel_message_id, "10")
        self.assertEqual(first.metadata["chat_type"], "private")
        self.assertEqual(
            http.gets[0][0],
            "https://api.telegram.org/bottoken-1/getUpdates",
        )
        self.assertEqual(http.gets[0][1]["params"]["timeout"], 25)
        # The first poll names no offset; each later one acknowledges
        # every update seen so far (max id + 1, across pages).
        offsets = [
            kwargs["params"].get("offset") for _, kwargs in http.gets
        ]
        self.assertEqual(offsets, [None, 3, 11])
        self.assertEqual(channel.status.state, "connected")

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_allowed_chat_ids_filter(self) -> None:
        channel = _channel(allowed=["111"])
        http = _FakeTelegramHTTP(
            pages=[_page(_update(1), _update(2, chat_id=222))],
        )
        received: list[ChannelEvent] = []
        task = await _drive(
            channel,
            http,
            until=lambda: len(http.gets) >= 2,
            received=received,
        )
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual([event.chat_id for event in received], ["111"])
        # The dropped update is still acknowledged, never re-polled.
        self.assertEqual(http.gets[1][1]["params"]["offset"], 3)

    async def test_non_text_updates_are_skipped_but_acknowledged(
        self,
    ) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP(
            pages=[
                _page(
                    {"update_id": 5, "callback_query": {"id": "cb"}},
                    _update(6, text="  "),
                    {
                        "update_id": 7,
                        "message": {
                            "message_id": 70,
                            "chat": {"id": 111},
                            "photo": [{"file_id": "f"}],
                        },
                    },
                    _update(8, text="kept"),
                    {
                        "update_id": 9,
                        "message": {"message_id": 90, "text": "orphan"},
                    },
                ),
            ],
        )
        received: list[ChannelEvent] = []
        task = await _drive(
            channel,
            http,
            until=lambda: len(http.gets) >= 2,
            received=received,
        )
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual([event.message for event in received], ["kept"])
        self.assertEqual(http.gets[1][1]["params"]["offset"], 10)

    async def test_poll_error_logs_and_retries(self) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP(pages=[_page(_update(1))], failures=1)
        received: list[ChannelEvent] = []
        with (
            patch.object(telegram_module, "_ERROR_RETRY_SECONDS", 0),
            self.assertLogs("as", level="WARNING") as captured,
        ):
            task = await _drive(
                channel,
                http,
                until=lambda: bool(received),
                received=received,
            )
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # One failed poll, then the retry delivered the page (the
        # listener may already be parked on the poll after it).
        self.assertGreaterEqual(len(http.gets), 2)
        self.assertEqual([event.message for event in received], ["hello"])
        self.assertIn("retrying", "\n".join(captured.output))
        self.assertEqual(channel.status.state, "stopped")
        self.assertTrue(http.closed)

    async def test_rejected_api_body_is_retried(self) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP(
            pages=[_rejected_page(), _page(_update(1))],
        )
        states: list[str] = []
        http.on_get = lambda: states.append(channel.status.state)
        received: list[ChannelEvent] = []
        with (
            patch.object(telegram_module, "_ERROR_RETRY_SECONDS", 0),
            self.assertLogs("as", level="WARNING") as captured,
        ):
            task = await _drive(
                channel,
                http,
                until=lambda: bool(received),
                received=received,
            )
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # connecting → retrying (rejected body) → connected (delivery).
        self.assertEqual(states[:3], ["connecting", "retrying", "connected"])
        self.assertEqual([event.message for event in received], ["hello"])
        self.assertIn("Unauthorized", "\n".join(captured.output))
        self.assertEqual(channel.status.state, "stopped")

    async def test_cancel_during_long_poll_exits_cleanly(self) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP(pages=[_page()])
        received: list[ChannelEvent] = []
        task = await _drive(
            channel,
            http,
            until=lambda: len(http.gets) >= 2,
            received=received,
        )

        self.assertEqual(channel.status.state, "connected")
        self.assertEqual(received, [])

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(channel.status.state, "stopped")
        self.assertTrue(http.closed)


class TelegramSendResponseTest(IsolatedAsyncioTestCase):
    """Reply accumulation and sendMessage delivery."""

    async def test_reply_is_sent_as_one_plain_text_message(self) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP()

        await _send(channel, http, _event_stream("hello from agent"))

        self.assertEqual(len(http.posts), 1)
        url, kwargs = http.posts[0]
        self.assertEqual(
            url,
            "https://api.telegram.org/bottoken-1/sendMessage",
        )
        self.assertEqual(
            kwargs["json"],
            {"chat_id": "111", "text": "hello from agent"},
        )
        self.assertTrue(http.closed)

    async def test_long_reply_splits_on_a_newline_boundary(self) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP()
        text = "x" * 3_900 + "\n" + "y" * 1_000

        await _send(channel, http, _event_stream(text))

        sent = [kwargs["json"]["text"] for _, kwargs in http.posts]
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(len(part) <= 4096 for part in sent))
        self.assertEqual(sent[0], "x" * 3_900)
        self.assertEqual(sent[1], "y" * 1_000)
        self.assertEqual(
            [kwargs["json"]["chat_id"] for _, kwargs in http.posts],
            ["111", "111"],
        )

    async def test_unsplittable_reply_hard_splits_at_the_limit(self) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP()

        await _send(channel, http, _event_stream("x" * 8_200))

        sent = [kwargs["json"]["text"] for _, kwargs in http.posts]
        self.assertEqual([len(part) for part in sent], [4096, 4096, 8])
        self.assertEqual(sent[0], "x" * 4096)
        self.assertEqual(sent[2], "x" * 8)

    async def test_confirmation_request_is_sent_as_plain_text(self) -> None:
        channel = _channel()
        http = _FakeTelegramHTTP()

        await _send(channel, http, _confirmation_stream())

        # No text accumulated, so the base's placeholder went out first
        # (same as the other channels), then the approval request.
        self.assertEqual(len(http.posts), 2)
        self.assertEqual(
            http.posts[0][1]["json"]["text"],
            _NO_TEXT_REPLY,
        )
        payload = http.posts[1][1]["json"]
        self.assertEqual(payload["chat_id"], "111")
        self.assertIn("Bash", payload["text"])
        self.assertIn("approval", payload["text"])
        self.assertIn('{"command":"ls"}', payload["text"])
