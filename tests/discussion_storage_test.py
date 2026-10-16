# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Discussion record persistence on both storage backends."""
from contextlib import AsyncExitStack
from datetime import datetime
from unittest import IsolatedAsyncioTestCase, TestCase

import fakeredis.aioredis

from agentscope.app.storage import (
    AsyncSQLAlchemyStorage,
    ChatModelConfig,
    DebaterConfig,
    DiscussionData,
    DiscussionRecord,
    DiscussionStatus,
    JudgeConfig,
    RedisStorage,
    StorageBase,
)


def make_storage() -> RedisStorage:
    """Create a RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def _chat_model() -> ChatModelConfig:
    """A minimal but complete chat model config."""
    return ChatModelConfig(
        type="openai",
        credential_id="cred-1",
        model="gpt-4o",
        parameters={},
    )


def _discussion_record(
    user_id: str = "user-1",
    status: DiscussionStatus = DiscussionStatus.RUNNING,
    created_at: datetime | None = None,
) -> DiscussionRecord:
    """A two-debater discussion record."""
    record = DiscussionRecord(
        user_id=user_id,
        data=DiscussionData(
            topic="cats vs dogs",
            debaters=[
                DebaterConfig(
                    name="pro",
                    system_prompt="You argue for cats.",
                    chat_model_config=_chat_model(),
                ),
                DebaterConfig(
                    name="con",
                    system_prompt="You argue for dogs.",
                    chat_model_config=_chat_model(),
                ),
            ],
            judge=JudgeConfig(chat_model_config=_chat_model()),
            status=status,
        ),
    )
    if created_at is not None:
        record.created_at = created_at
    return record


class _DiscussionStorageContractMixin:
    """Backend-agnostic discussion storage behaviours.

    Concrete classes pair this with
    :class:`~unittest.async_case.IsolatedAsyncioTestCase` and implement
    :meth:`_make_storage`, so every test below runs once per backend and
    keeps the Redis and SQL implementations honest about the same
    contract.
    """

    async def _make_storage(self) -> StorageBase:
        """Return the backend under test, ready for use."""
        raise NotImplementedError

    async def test_discussion_round_trip(self) -> None:
        storage = await self._make_storage()
        saved = await storage.upsert_discussion(
            "user-1",
            _discussion_record(),
        )
        fetched = await storage.get_discussion("user-1", saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.data.status, DiscussionStatus.RUNNING)
        self.assertEqual(fetched.data.topic, "cats vs dogs")
        self.assertEqual(
            [d.name for d in fetched.data.debaters],
            ["pro", "con"],
        )

    async def test_discussions_list_newest_first(self) -> None:
        storage = await self._make_storage()
        days = (
            datetime(2026, 1, 3),
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
        )
        ids = []
        for day in days:
            saved = await storage.upsert_discussion(
                "user-1",
                _discussion_record(created_at=day),
            )
            ids.append(saved.id)
        # Insertion order (Jan 3, Jan 1, Jan 2) differs from creation
        # order, so only a real ``created_at`` sort can pass here.
        self.assertEqual(
            [r.id for r in await storage.list_discussions("user-1")],
            [ids[0], ids[2], ids[1]],
        )

    async def test_discussions_filter_by_status(self) -> None:
        storage = await self._make_storage()
        seeded = {
            status: await storage.upsert_discussion(
                "user-1",
                _discussion_record(status=status),
            )
            for status in (
                DiscussionStatus.RUNNING,
                DiscussionStatus.COMPLETED,
                DiscussionStatus.FAILED,
            )
        }
        for status, saved in seeded.items():
            self.assertEqual(
                [
                    record.id
                    for record in await storage.list_discussions(
                        "user-1",
                        status=status,
                    )
                ],
                [saved.id],
            )
        # No filter: every seeded discussion comes back.
        self.assertEqual(
            len(await storage.list_discussions("user-1")),
            len(seeded),
        )

    async def test_owner_scoping(self) -> None:
        storage = await self._make_storage()
        discussion = await storage.upsert_discussion(
            "user-1",
            _discussion_record(),
        )
        self.assertIsNone(
            await storage.get_discussion("user-2", discussion.id),
        )
        self.assertEqual(
            await storage.list_discussions("user-2"),
            [],
        )
        # Deleting as the wrong owner must not remove the discussion.
        self.assertFalse(
            await storage.delete_discussion("user-2", discussion.id),
        )
        self.assertIsNotNone(
            await storage.get_discussion("user-1", discussion.id),
        )

    async def test_delete(self) -> None:
        storage = await self._make_storage()
        discussion = await storage.upsert_discussion(
            "user-1",
            _discussion_record(),
        )
        self.assertTrue(
            await storage.delete_discussion("user-1", discussion.id),
        )
        self.assertFalse(
            await storage.delete_discussion("user-1", discussion.id),
        )
        self.assertEqual(
            await storage.list_discussions("user-1"),
            [],
        )


class RedisDiscussionStorageTest(
    _DiscussionStorageContractMixin,
    IsolatedAsyncioTestCase,
):
    """Upsert / get / list / delete over fakeredis."""

    async def _make_storage(self) -> StorageBase:
        return make_storage()


class DiscussionStatusColumnLengthTest(TestCase):
    """``discussions.status`` is a ``String(32)`` column.

    SQLite silently ignores string lengths, but Postgres and MySQL
    enforce them — a longer enum value would only fail in production.
    """

    def test_status_values_fit_the_column(self) -> None:
        self.assertLessEqual(
            max(len(s.value) for s in DiscussionStatus),
            32,
        )


class SqlDiscussionStorageTest(
    _DiscussionStorageContractMixin,
    IsolatedAsyncioTestCase,
):
    """Same behaviours over in-memory SQLite."""

    async def asyncSetUp(self) -> None:
        # ``:memory:`` gives a private DB per connection; per-test
        # isolation is what we want here.
        self._stack = AsyncExitStack()
        self.storage = await self._stack.enter_async_context(
            AsyncSQLAlchemyStorage(
                "sqlite+aiosqlite:///:memory:",
                create_tables=True,
            ),
        )

    async def asyncTearDown(self) -> None:
        await self._stack.aclose()

    async def _make_storage(self) -> StorageBase:
        return self.storage
