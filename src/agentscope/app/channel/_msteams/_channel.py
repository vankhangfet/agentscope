# -*- coding: utf-8 -*-
"""MS Teams channel (push-only, via an Office 365 Incoming Webhook).

An Incoming Webhook is one-way: the channel POSTs a JSON MessageCard
to a connector URL tied to one Teams channel and Teams renders it —
there is no inbound source, so ``start_listening`` only parks. The
URL is also the whole credential (no auth header); ``send_response``
cards a reply there, and the service's notification tool rides
``send_notification`` directly.
"""
import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable, TYPE_CHECKING

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

# Teams renders very large MessageCards poorly; ~16k characters is the
# practical ceiling, past which the text is cut with a marker.
_MAX_TEXT = 16_384
_TRUNCATED_MARKER = "\n… (truncated)"
_HTTP_TIMEOUT_SECONDS = 10.0
_THEME_COLOR = "0076D7"


def _cap_text(text: str) -> str:
    """Cap card text at the MessageCard limit, marking a cut.

    Args:
        text (`str`): The card body.

    Returns:
        `str`: ``text`` unchanged when within the limit, else the head
        of it plus a truncation marker, keeping the total in limit.
    """
    if len(text) <= _MAX_TEXT:
        return text
    return text[: _MAX_TEXT - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER


class MSTeamsChannel(ChannelBase):
    """MS Teams push-notification channel (Incoming Webhook)."""

    channel_type = "msteams"
    display_name = "MS Teams"
    description = (
        "Push notifications to a Teams channel via an Incoming Webhook."
    )
    icon_url = (
        "https://www.google.com/s2/favicons?domain=teams.microsoft.com"
        "&sz=128"
    )
    # One connector URL identifies one Teams channel, so it doubles as
    # the bot identity the registry checks for double-binding.
    platform_bot_id_field = "webhook_url"

    class Credentials(BaseModel):
        """MS Teams webhook credentials."""

        webhook_url: str = Field(
            title="Webhook URL",
            description="Incoming Webhook URL copied from the Teams "
            "channel's Connectors settings; the URL is itself the "
            "secret (anyone holding it can post), so it is masked",
            json_schema_extra={"format": "password"},
        )

    class Config(BaseModel):
        """MS Teams platform options (none in v1)."""

    capabilities = ChannelCapability(
        text=True,
        max_message_length=_MAX_TEXT,
    )

    def __init__(
        self,
        channel_id: str,
        credentials: "MSTeamsChannel.Credentials",
        config: "MSTeamsChannel.Config",
    ) -> None:
        """Read the webhook URL from the validated models.

        Args:
            channel_id (`str`):
                This channel instance's unique id.
            credentials (`MSTeamsChannel.Credentials`):
                Validated webhook credentials (the connector URL).
            config (`MSTeamsChannel.Config`):
                Validated platform options (none in v1).
        """
        self._channel_id = channel_id
        self._webhook_url = credentials.webhook_url
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
        """Store ``emit`` and park — the webhook is one-way, so there is
        nothing to connect to and nothing to receive; cancellation is
        the exit.

        Args:
            emit (`Callable`): Gateway callback (never called).
        """
        self._emit = emit
        self.status.state = "connected"
        self.status.last_error = ""
        try:
            await asyncio.Event().wait()
        finally:
            self.status.state = "stopped"

    # -- Outbound --

    async def send_response(  # pylint: disable=unused-argument
        self,
        event: ChannelEvent,
        events: AsyncIterator[dict],
    ) -> None:
        """Accumulate the reply (non-streaming) and POST it as cards —
        one per text block — plus a plain-text approval card when a run
        parks.

        Args:
            event (`ChannelEvent`): The send target (nominal — one
                fixed webhook per instance).
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
        # The empty v1 Config has no flags, so thinking and tool
        # process stay hidden (the _render defaults).
        for block in self._render(reply):
            if isinstance(block, TextBlock):
                if block.text:
                    await self.send_notification(None, block.text)
            else:
                logger.debug(
                    "MS Teams '%s' cannot deliver a data block",
                    self._channel_id,
                )
        if confirm is not None:
            await self._present_confirm(confirm)

    async def _present_confirm(
        self,
        req: RequireUserConfirmEvent,
    ) -> None:
        """POST one plain-text approval card per tool call — an Incoming
        Webhook card's actions cannot report a decision back to the
        gateway, so v1 asks in text (approve via the channel that
        started the run).

        Args:
            req (`RequireUserConfirmEvent`): The approval request to show.
        """
        for tool in req.tool_calls:
            await self.send_notification(
                tool.name,
                "🛡️ Tool execution needs approval\n"
                f"Tool: {tool.name}\n"
                f"Arguments: {str(tool.input)[:800]}",
            )

    async def send_notification(
        self,
        title: str | None,
        text: str,
    ) -> None:
        """POST one MessageCard to the webhook URL.

        Args:
            title (`str | None`): Card title; ``None`` omits it.
            text (`str`): Card body, capped at the MessageCard limit.

        Raises:
            `RuntimeError`: If the webhook is unreachable or rejects
                the card; the caller renders the message.
        """
        card: dict[str, Any] = {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": _THEME_COLOR,
            "text": _cap_text(text),
        }
        if title:
            card["title"] = title
        client = await self._ensure_http()
        try:
            resp = await client.post(self._webhook_url, json=card)
        except Exception as e:  # pylint: disable=broad-except
            raise RuntimeError(f"Teams webhook unreachable: {e}") from e
        if not 200 <= resp.status_code < 300:
            body = str(resp.text or "")[:200]
            raise RuntimeError(
                f"Teams webhook rejected: {resp.status_code} {body}",
            )
        logger.info(
            "MS Teams '%s' notification delivered",
            self._channel_id,
        )

    # -- Helpers --

    def _new_http_client(self) -> "httpx.AsyncClient":
        """Create the asynchronous HTTP client used by this channel."""
        import httpx

        return httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
        )

    async def _ensure_http(self) -> "httpx.AsyncClient":
        """Return the lazy HTTP client, shared by every POST.

        An instance built by ``ChannelClients`` never runs
        ``start_listening``, yet must still reach the webhook — so the
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

        The idle connection loop opens nothing (the webhook is
        one-way); this covers the sender-only instances built by
        ``ChannelClients``, which never run it.
        """
        if self._http is not None:
            await self._http.aclose()
            self._http = None
