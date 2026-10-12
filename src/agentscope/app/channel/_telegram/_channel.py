# -*- coding: utf-8 -*-
"""Telegram channel (Bot API long polling, pure httpx).

No SDK: the listener long-polls ``getUpdates`` on the app event loop.
Each page advances the ``offset`` — acknowledging every update, acted on
or not — and each text message is normalised into a ``ChannelEvent``
and emitted. Outbound replies are accumulated once and sent as plain
``sendMessage`` text, chunked to Telegram's 4096-character cap;
``parse_mode`` is left off (Markdown escaping is error-prone) and an
approval is a plain-text request — Telegram shows no buttons without
inline keyboards (a v1 non-goal).
"""
import asyncio
from typing import AsyncIterator, Awaitable, Callable, TYPE_CHECKING

from pydantic import BaseModel, Field

from ...._logging import logger
from ....event import ReplyEndEvent, RequireUserConfirmEvent
from ....message import Msg, TextBlock
from .._base import (
    ChannelBase,
    ChannelCapability,
    ChannelEvent,
    ChannelConfirmationResultEvent,
    ChannelStatus,
    _EVENT_ADAPTER,
)

if TYPE_CHECKING:
    import httpx

_TELEGRAM_API = "https://api.telegram.org"
# Telegram's hard limit is 4096 characters per message.
_MAX_LEN = 4096
# Seconds the server holds an empty long poll, and the client-side cap
# around it (server hold plus transport slack).
_POLL_SECONDS = 25
_POLL_TIMEOUT_SECONDS = 30.0
# Pause after a failed poll — log, sleep, and poll again (the
# self-healing reconnect the base class's contract asks for).
_ERROR_RETRY_SECONDS = 3.0
# How far back from the length cap to look for a newline to split on.
_SPLIT_LOOKBACK = 400


class TelegramChannel(ChannelBase):
    """Telegram platform channel (Bot API, long polling)."""

    channel_type = "telegram"
    display_name = "Telegram"
    description = "Bot for Telegram chats, groups and direct messages."
    icon_url = "https://www.google.com/s2/favicons?domain=telegram.org&sz=128"
    platform_bot_id_field = "bot_token"

    class Credentials(BaseModel):
        """Telegram bot credentials."""

        bot_token: str = Field(
            title="Bot Token",
            description="Bot token issued by @BotFather",
            json_schema_extra={"format": "password"},
        )

    class Config(BaseModel):
        """Telegram platform options."""

        allowed_chat_ids: list[str] = Field(
            default_factory=list,
            title="Allowed chat IDs",
            description="Chat IDs the bot answers; empty accepts every "
            "chat",
        )
        show_tool_process: bool = Field(
            default=False,
            title="Show tool process",
            description="Show tool calls and results inline in the reply",
        )
        show_thinking: bool = Field(
            default=False,
            title="Show thinking",
            description="Show the model's reasoning inline in the reply",
        )

    capabilities = ChannelCapability(
        text=True,
        max_message_length=_MAX_LEN,
    )

    def __init__(
        self,
        channel_id: str,
        credentials: "TelegramChannel.Credentials",
        config: "TelegramChannel.Config",
    ) -> None:
        """Read the bot token and options from the validated models.

        Args:
            channel_id (`str`):
                This channel instance's unique id.
            credentials (`TelegramChannel.Credentials`):
                Validated bot credentials (the BotFather token).
            config (`TelegramChannel.Config`):
                Validated platform options.
        """
        self._channel_id = channel_id
        self._bot_token = credentials.bot_token
        self._config = config
        self.status = ChannelStatus()
        self._http: "httpx.AsyncClient | None" = None
        self._http_lock = asyncio.Lock()

    @property
    def channel_id(self) -> str:
        """The unique channel instance identifier."""
        return self._channel_id

    # -- Lifecycle --

    async def start_listening(
        self,
        emit: Callable[
            [ChannelEvent | ChannelConfirmationResultEvent],
            Awaitable[None],
        ],
    ) -> None:
        """Long-poll ``getUpdates`` until cancelled, acknowledging each
        page via the offset and emitting its text messages, and close
        the HTTP client on exit.

        Args:
            emit (`Callable`): Gateway callback for inbound events.
        """
        self._emit = emit
        self.status.state = "connecting"
        offset = 0
        try:
            while True:
                try:
                    for update in await self._get_updates(offset):
                        update_id = update.get("update_id")
                        if isinstance(update_id, int):
                            offset = max(offset, update_id + 1)
                        await self._dispatch(update)
                    self.status.state = "connected"
                    self.status.last_error = ""
                except Exception as e:  # pylint: disable=broad-except
                    self.status.state = "retrying"
                    self.status.last_error = str(e)
                    logger.warning(
                        "Telegram '%s' getUpdates failed, retrying in "
                        "%.0fs: %s",
                        self._channel_id,
                        _ERROR_RETRY_SECONDS,
                        e,
                    )
                    await asyncio.sleep(_ERROR_RETRY_SECONDS)
        finally:
            self.status.state = "stopped"
            if self._http is not None:
                await self._http.aclose()
                self._http = None

    async def _dispatch(self, update: dict) -> None:
        """Normalise one update and emit it; a failure never stops the
        polling loop.

        Args:
            update (`dict`): One entry from a getUpdates page.
        """
        try:
            event = self._normalize(update)
            if event is not None and self._emit is not None:
                await self._emit(event)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Telegram '%s' update handling failed",
                self._channel_id,
            )

    def _normalize(self, update: dict) -> ChannelEvent | None:
        """Convert one Telegram update into a ``ChannelEvent``.

        Only text messages become events (v1). Non-message updates
        (callback queries, edited messages), media messages and empty
        text are dropped, as are chats outside ``allowed_chat_ids`` —
        the caller's offset advance acknowledges them regardless, so
        they are never re-polled.

        Args:
            update (`dict`): One entry from a getUpdates page.

        Returns:
            `ChannelEvent | None`: The normalised event, or ``None``
            when there is nothing to act on.
        """
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = str(message.get("text") or "").strip()
        if not text:
            return None
        chat = message.get("chat")
        chat_id = str(chat.get("id") or "") if isinstance(chat, dict) else ""
        if not chat_id:
            return None
        allowed = self._config.allowed_chat_ids
        if allowed and chat_id not in allowed:
            return None
        sender = message.get("from")
        sender = sender if isinstance(sender, dict) else {}
        return ChannelEvent(
            channel_id=self._channel_id,
            channel_user_id=str(sender.get("id") or ""),
            channel_user_name=str(sender.get("first_name") or ""),
            chat_id=chat_id,
            channel_message_id=str(message.get("message_id") or "") or None,
            content=[TextBlock(text=text)],
            metadata={"chat_type": str(chat.get("type", ""))},
        )

    # -- Outbound --

    async def send_response(
        self,
        event: ChannelEvent,
        events: AsyncIterator[dict],
    ) -> None:
        """Accumulate the reply (non-streaming) and send it as plain
        text messages; a parked run gets a plain-text approval request.

        Args:
            event (`ChannelEvent`): The send target (chat id).
            events (`AsyncIterator[dict]`): The run's session events.
        """
        reply: Msg | None = None
        confirm: RequireUserConfirmEvent | None = None
        async for raw in events:
            evt = _EVENT_ADAPTER.validate_python(raw)
            if isinstance(evt, RequireUserConfirmEvent):
                confirm = evt
                break
            reply_id = getattr(evt, "reply_id", None)
            if reply_id is not None:
                if reply is None:
                    reply = Msg(name="assistant", role="assistant", content=[])
                    reply.id = reply_id
                reply.append_event(evt)
            if isinstance(evt, ReplyEndEvent):
                break
        for block in self._render(
            reply,
            show_thinking=self._config.show_thinking,
            show_tool_process=self._config.show_tool_process,
        ):
            if isinstance(block, TextBlock):
                for part in self._split_long_message(block.text):
                    if part:
                        await self._send_message(event.chat_id, part)
            else:
                logger.debug(
                    "Telegram '%s' cannot deliver a data block",
                    self._channel_id,
                )
        if confirm is not None:
            await self._present_confirm(event, confirm)

    async def _send_message(self, chat_id: str, text: str) -> None:
        """POST one plain-text ``sendMessage`` (no parse_mode in v1).

        Args:
            chat_id (`str`): The chat to send to.
            text (`str`): The message text, within the length cap.
        """
        client = await self._ensure_http()
        resp = await client.post(
            self._api_url("sendMessage"),
            json={"chat_id": chat_id, "text": text},
        )
        data = resp.json()
        if data.get("ok") is not True:
            logger.warning(
                "Telegram '%s' sendMessage to '%s' failed: %s",
                self._channel_id,
                chat_id,
                data.get("description", data),
            )

    async def _present_confirm(
        self,
        event: ChannelEvent,
        req: RequireUserConfirmEvent,
    ) -> None:
        """Send one plain-text approval request per tool call — Telegram
        has no buttons without inline keyboards (a v1 non-goal).

        Args:
            event (`ChannelEvent`): The send target, for its ``chat_id``.
            req (`RequireUserConfirmEvent`): The approval request to show.
        """
        for tool in req.tool_calls:
            await self._send_message(
                event.chat_id,
                "🛡️ Tool execution needs approval\n"
                f"Tool: {tool.name}\n"
                f"Arguments: {str(tool.input)[:800]}",
            )

    # -- Helpers --

    async def _get_updates(self, offset: int) -> list[dict]:
        """One long poll; returns the page's updates.

        Args:
            offset (`int`): The first unacknowledged update id — 0 names
                none, so the server starts at the earliest pending.

        Returns:
            `list[dict]`: The page's raw updates.
        """
        client = await self._ensure_http()
        params: dict[str, int] = {"timeout": _POLL_SECONDS}
        if offset:
            params["offset"] = offset
        resp = await client.get(
            self._api_url("getUpdates"),
            params=params,
        )
        data = resp.json()
        if data.get("ok") is not True:
            raise RuntimeError(f"getUpdates rejected: {data}")
        result = data.get("result")
        return result if isinstance(result, list) else []

    def _api_url(self, method: str) -> str:
        """The Bot API URL for one method (the token rides the path).

        Args:
            method (`str`): The Bot API method name.

        Returns:
            `str`: The full endpoint URL.
        """
        return f"{_TELEGRAM_API}/bot{self._bot_token}/{method}"

    def _new_http_client(self) -> "httpx.AsyncClient":
        """Create the asynchronous HTTP client used by this channel."""
        import httpx

        return httpx.AsyncClient(timeout=httpx.Timeout(_POLL_TIMEOUT_SECONDS))

    async def _ensure_http(self) -> "httpx.AsyncClient":
        """Return the lazy HTTP client, shared by listener and sender.

        An instance built by ``ChannelClients`` never runs
        ``start_listening``, yet must still reach the platform — so the
        client is created on first use, not during connection setup.
        Double-checked under a lock: two concurrent callers would each
        build a client and orphan whichever lost the race (nothing would
        ever close it).
        """
        if self._http is None:
            async with self._http_lock:
                if self._http is None:
                    self._http = self._new_http_client()
        return self._http

    async def aclose(self) -> None:
        """Close the HTTP client this instance opened lazily.

        The connection loop closes its own in ``start_listening``; this
        covers the sender-only instances, which never run it.
        """
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _split_long_message(self, text: str) -> list[str]:
        """Split text into chunks within Telegram's length limit,
        preferring a newline boundary near the cut point.

        Args:
            text (`str`): The text to split.
        """
        limit = self.capabilities.max_message_length
        if len(text) <= limit:
            return [text]
        parts: list[str] = []
        remaining = text
        while len(remaining) > limit:
            cut = remaining.rfind("\n", limit - _SPLIT_LOOKBACK, limit)
            if cut <= 0:
                parts.append(remaining[:limit])
                remaining = remaining[limit:]
            else:
                parts.append(remaining[:cut])
                remaining = remaining[cut + 1 :]
        if remaining:
            parts.append(remaining)
        return parts
