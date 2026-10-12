# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The send_teams_notification chat tool, over fakeredis storage.

Real channel records are seeded through ``upsert_channel``; the webhook
POST is faked at the httpx boundary (the same fake the MSTeamsChannel
suite uses), so the tool is exercised end to end — record lookup,
channel construction, card POST, and client cleanup. The
toolkit-registration test at the bottom exercises the ``get_toolkit``
assembly path.
"""
from typing import Any
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import fakeredis.aioredis

from utils import FakeWorkspaceManager
from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app._manager import BackgroundTaskManager, SchedulerManager
from agentscope.app._service import ResourceAccessService, get_toolkit
from agentscope.app._tool import TeamsNotify
from agentscope.app.access import DenyAllResourceAccessPolicy
from agentscope.app.channel import MSTeamsChannel
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    ChannelBinding,
    ChannelRecord,
    ChatModelConfig,
    RedisStorage,
    RoutingConfig,
    SessionConfig,
    SessionRecord,
    SessionSettings,
)
from agentscope.message import TextBlock, ToolResultState

USER = "user-1"
_WEBHOOK_URL = "https://team.webhook.office.com/webhookb2/abc/def"
_OTHER_WEBHOOK_URL = "https://team.webhook.office.com/webhookb2/xyz/uvw"


def make_storage() -> RedisStorage:
    """A RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


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


async def _seed_channel(
    storage: RedisStorage,
    *,
    channel_type: str = "msteams",
    webhook_url: str = _WEBHOOK_URL,
    platform_bot_id: str = _WEBHOOK_URL,
    name: str | None = None,
) -> ChannelRecord:
    """Persist one channel record and return it."""
    credentials = (
        {"webhook_url": webhook_url}
        if channel_type == "msteams"
        else {"bot_token": "123:abc"}
    )
    record = ChannelRecord(
        user_id=USER,
        channel_type=channel_type,
        name=name,
        credentials=credentials,
        routing=RoutingConfig(
            bindings=[ChannelBinding(agent_id="agent-1")],
        ),
        session=SessionSettings(chat_model_config={}),
    )
    await storage.upsert_channel(record, platform_bot_id)
    return record


def _text(chunk: Any) -> str:
    """The chunk's text content, joined across blocks."""
    return "\n".join(
        block.text for block in chunk.content
        if isinstance(block, TextBlock)
    )


def _cards(http: _FakeTeamsHTTP) -> list[dict[str, Any]]:
    """The posted MessageCards, in order."""
    return [kwargs["json"] for _, kwargs in http.posts]


class TeamsNotifyToolTest(IsolatedAsyncioTestCase):
    """Look up the channel, post the card, answer — never raise."""

    async def asyncSetUp(self) -> None:
        self.storage = make_storage()

    def _tool(self) -> TeamsNotify:
        """The tool under test, bound to the seeded storage."""
        return TeamsNotify(user_id=USER, storage=self.storage)

    def _notify(
        self,
        http: _FakeTeamsHTTP,
        **kwargs: Any,
    ) -> Any:
        """Call the tool with the fake httpx client injected into every
        channel the tool constructs (class-level patch)."""
        tool = self._tool()

        async def _call() -> Any:
            with patch.object(
                MSTeamsChannel,
                "_new_http_client",
                return_value=http,
            ):
                return await tool(**kwargs)

        return _call()

    async def test_no_channel_is_a_friendly_error(self) -> None:
        """A user with no channels at all gets the prescribed error,
        naming the Channels page — not an exception."""
        chunk = await self._notify(_FakeTeamsHTTP(), text="hi")

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        text = _text(chunk)
        self.assertIn("No MS Teams channel is configured", text)
        self.assertIn("Channels page", text)

    async def test_non_msteams_channels_are_ignored(self) -> None:
        """Only msteams records count: other channel types neither
        satisfy the lookup nor receive a post."""
        await _seed_channel(
            self.storage,
            channel_type="telegram",
            platform_bot_id="tg-1",
        )
        http = _FakeTeamsHTTP()

        chunk = await self._notify(http, text="hi")

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        self.assertIn("No MS Teams channel is configured", _text(chunk))
        self.assertEqual(http.posts, [])

    async def test_happy_path_posts_the_card(self) -> None:
        """A titled notification is POSTed as a MessageCard to the
        stored webhook URL and answered with the success text."""
        await _seed_channel(self.storage)
        http = _FakeTeamsHTTP()

        chunk = await self._notify(
            http,
            title="Build failed",
            text="deploy 42 broke",
        )

        self.assertEqual(chunk.state, ToolResultState.SUCCESS)
        self.assertIn(
            "Notification delivered to MS Teams.",
            _text(chunk),
        )
        self.assertEqual(len(http.posts), 1)
        url, kwargs = http.posts[0]
        self.assertEqual(url, _WEBHOOK_URL)
        card = kwargs["json"]
        self.assertEqual(card["title"], "Build failed")
        self.assertEqual(card["text"], "deploy 42 broke")
        # The lazily opened httpx client is closed after the send.
        self.assertTrue(http.closed)

    async def test_empty_title_sends_an_untitled_card(self) -> None:
        """The default empty title is passed through as ``None``, so
        the card carries no title."""
        await _seed_channel(self.storage)
        http = _FakeTeamsHTTP()

        chunk = await self._notify(http, text="plain note")

        self.assertEqual(chunk.state, ToolResultState.SUCCESS)
        card = _cards(http)[0]
        self.assertNotIn("title", card)
        self.assertEqual(card["text"], "plain note")

    async def test_webhook_failure_is_an_error_chunk(self) -> None:
        """A webhook rejection surfaces as an error chunk carrying the
        channel's message."""
        await _seed_channel(self.storage)
        http = _FakeTeamsHTTP(status_code=400, body="Bad Request")

        chunk = await self._notify(http, title="t", text="x")

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        text = _text(chunk)
        self.assertIn("Teams webhook rejected", text)
        self.assertIn("400", text)
        self.assertIn("Bad Request", text)

    async def test_multiple_channels_use_the_first_with_a_note(self) -> None:
        """With several msteams channels, exactly one card is POSTed —
        to whichever record lists first — and the success chunk says
        so."""
        await _seed_channel(self.storage, name="primary")
        await _seed_channel(
            self.storage,
            webhook_url=_OTHER_WEBHOOK_URL,
            platform_bot_id=_OTHER_WEBHOOK_URL,
            name="second",
        )
        http = _FakeTeamsHTTP()

        chunk = await self._notify(http, text="hi")

        self.assertEqual(chunk.state, ToolResultState.SUCCESS)
        text = _text(chunk)
        self.assertIn("Notification delivered to MS Teams.", text)
        self.assertIn("2 MS Teams channels", text)
        records = await self.storage.list_channels(USER)
        first = next(
            record
            for record in records
            if record.channel_type == "msteams"
        )
        self.assertIn(first.name or first.id, text)
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(
            http.posts[0][0],
            first.credentials["webhook_url"],
        )

    async def test_broken_credentials_are_an_error_chunk(self) -> None:
        """A record whose credentials no longer validate answers with
        an error chunk instead of raising."""
        record = ChannelRecord(
            user_id=USER,
            channel_type="msteams",
            credentials={},  # webhook_url missing
            routing=RoutingConfig(
                bindings=[ChannelBinding(agent_id="agent-1")],
            ),
            session=SessionSettings(chat_model_config={}),
        )
        await self.storage.upsert_channel(record, "bot-1")

        chunk = await self._tool()(text="hi")

        self.assertEqual(chunk.state, ToolResultState.ERROR)
        self.assertIn("webhook_url", _text(chunk))


# ----------------------------------------------------------------------
# Toolkit assembly
# ----------------------------------------------------------------------

class _FakeWorkspace:
    """Stand-in for a resolved workspace — only the three methods
    :func:`get_toolkit` calls are implemented."""

    async def list_tools(self) -> list:
        """No workspace tools."""
        return []

    async def list_skills(self, agent_id: str) -> list:
        """No workspace skills."""
        return []

    async def list_mcps(self, agent_id: str, session_id: str) -> list:
        """No workspace MCPs."""
        return []


class _NullBus:
    """MessageBus placeholder; nothing assembled here awaits it."""


class _NoOpStorage:
    """Storage placeholder; only list_agents is reached."""

    async def list_agents(self, _user_id: str) -> list:
        """No agents."""
        return []


def _tool_names(toolkit: Any) -> list[str]:
    """Every registered tool name, walking the tool groups."""
    return [t.name for group in toolkit.tool_groups for t in group.tools]


def _make_session(agent_id: str) -> SessionRecord:
    """A minimal session record with a model configured."""
    return SessionRecord(
        user_id=USER,
        agent_id=agent_id,
        config=SessionConfig(
            workspace_id="ws",
            chat_model_config=ChatModelConfig(
                type="dashscope_credential",
                credential_id="c",
                model="m",
                parameters={},
            ),
        ),
    )


class TeamsNotifyToolkitTest(IsolatedAsyncioTestCase):
    """get_toolkit attaches the notification tool unconditionally."""

    async def test_teams_notify_registered(self) -> None:
        """The tool lands in every assembled toolkit — it errors
        politely at call time when no channel exists."""
        agent = AgentRecord(
            user_id=USER,
            data=AgentData(
                name="A",
                system_prompt="You are A.",
                context_config=ContextConfig(),
                react_config=ReActConfig(),
            ),
        )
        session = _make_session(agent.id)
        storage = make_storage()

        toolkit = await get_toolkit(
            storage=storage,
            workspace=_FakeWorkspace(),  # type: ignore[arg-type]
            workspace_manager=FakeWorkspaceManager(),
            scheduler_manager=SchedulerManager(
                storage=_NoOpStorage(),  # type: ignore[arg-type]
                message_bus=_NullBus(),  # type: ignore[arg-type]
                workspace_manager=FakeWorkspaceManager(),
            ),
            background_task_manager=BackgroundTaskManager(
                message_bus=_NullBus(),  # type: ignore[arg-type]
            ),
            message_bus=_NullBus(),  # type: ignore[arg-type]
            middlewares=[],
            user_id=USER,
            agent_record=agent,
            session_record=session,
            resource_access_service=ResourceAccessService(
                storage=_NoOpStorage(),  # type: ignore[arg-type]
                policy=DenyAllResourceAccessPolicy(),
            ),
        )

        self.assertIn("send_teams_notification", _tool_names(toolkit))
        registered = await toolkit.get_tool("send_teams_notification")
        self.assertIsInstance(registered, TeamsNotify)
        # Bound to the calling user's storage, ready to answer politely.
        chunk = await registered(text="hi")
        self.assertEqual(chunk.state, ToolResultState.ERROR)
        self.assertIn("No MS Teams channel is configured", _text(chunk))
