# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Factory ticket persistence on both storage backends."""
from contextlib import AsyncExitStack
from datetime import datetime
from unittest import IsolatedAsyncioTestCase, TestCase

import fakeredis.aioredis

from agentscope.app.storage import (
    AsyncSQLAlchemyStorage,
    ChatModelConfig,
    FactoryStatus,
    FactoryTicketData,
    FactoryTicketRecord,
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


def _factory_record(
    user_id: str = "user-1",
    status: FactoryStatus = FactoryStatus.RUNNING,
    created_at: datetime | None = None,
) -> FactoryTicketRecord:
    """A fresh factory ticket record."""
    record = FactoryTicketRecord(
        user_id=user_id,
        data=FactoryTicketData(
            title="ship the login page",
            description="Users cannot sign in.",
            chat_model_config=_chat_model(),
            status=status,
        ),
    )
    if created_at is not None:
        record.created_at = created_at
    return record


class _FactoryStorageContractMixin:
    """Backend-agnostic factory ticket storage behaviours.

    Concrete classes pair this with
    :class:`~unittest.async_case.IsolatedAsyncioTestCase` and implement
    :meth:`_make_storage`, so every test below runs once per backend and
    keeps the Redis and SQL implementations honest about the same
    contract.
    """

    async def _make_storage(self) -> StorageBase:
        """Return the backend under test, ready for use."""
        raise NotImplementedError

    async def test_ticket_round_trip(self) -> None:
        storage = await self._make_storage()
        saved = await storage.upsert_factory_ticket(
            "user-1",
            _factory_record(),
        )
        fetched = await storage.get_factory_ticket("user-1", saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.data.status, FactoryStatus.RUNNING)
        self.assertEqual(fetched.data.title, "ship the login page")
        self.assertEqual(fetched.data.mode.value, "COLLABORATIVE")
        self.assertEqual(
            fetched.data.chat_model_config.model,
            "gpt-4o",
        )

    async def test_tickets_list_newest_first(self) -> None:
        storage = await self._make_storage()
        days = (
            datetime(2026, 1, 3),
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
        )
        ids = []
        for day in days:
            saved = await storage.upsert_factory_ticket(
                "user-1",
                _factory_record(created_at=day),
            )
            ids.append(saved.id)
        # Insertion order (Jan 3, Jan 1, Jan 2) differs from creation
        # order, so only a real ``created_at`` sort can pass here.
        self.assertEqual(
            [
                r.id
                for r in await storage.list_factory_tickets("user-1")
            ],
            [ids[0], ids[2], ids[1]],
        )

    async def test_tickets_filter_by_status(self) -> None:
        storage = await self._make_storage()
        seeded = {
            status: await storage.upsert_factory_ticket(
                "user-1",
                _factory_record(status=status),
            )
            for status in (
                FactoryStatus.RUNNING,
                FactoryStatus.WAITING_HUMAN,
                FactoryStatus.COMPLETED,
                FactoryStatus.FAILED,
            )
        }
        for status, saved in seeded.items():
            self.assertEqual(
                [
                    record.id
                    for record in await storage.list_factory_tickets(
                        "user-1",
                        status=status,
                    )
                ],
                [saved.id],
            )
        # No filter: every seeded ticket comes back.
        self.assertEqual(
            len(await storage.list_factory_tickets("user-1")),
            len(seeded),
        )

    async def test_owner_scoping(self) -> None:
        storage = await self._make_storage()
        ticket = await storage.upsert_factory_ticket(
            "user-1",
            _factory_record(),
        )
        self.assertIsNone(
            await storage.get_factory_ticket("user-2", ticket.id),
        )
        self.assertEqual(
            await storage.list_factory_tickets("user-2"),
            [],
        )
        # Deleting as the wrong owner must not remove the ticket.
        self.assertFalse(
            await storage.delete_factory_ticket("user-2", ticket.id),
        )
        self.assertIsNotNone(
            await storage.get_factory_ticket("user-1", ticket.id),
        )

    async def test_delete(self) -> None:
        storage = await self._make_storage()
        ticket = await storage.upsert_factory_ticket(
            "user-1",
            _factory_record(),
        )
        self.assertTrue(
            await storage.delete_factory_ticket("user-1", ticket.id),
        )
        self.assertFalse(
            await storage.delete_factory_ticket("user-1", ticket.id),
        )
        self.assertEqual(
            await storage.list_factory_tickets("user-1"),
            [],
        )
        # The deleted ticket leaves the sweep's view too.
        self.assertEqual(
            [
                r.id
                for r in await storage.list_active_factory_tickets()
                if r.id == ticket.id
            ],
            [],
        )

    async def test_active_lifecycle(self) -> None:
        """PENDING → RUNNING → WAITING_HUMAN stay active; COMPLETED
        leaves. A parked ticket must remain sweepable — the human may
        never return."""
        storage = await self._make_storage()
        record = await storage.upsert_factory_ticket(
            "user-1",
            _factory_record(status=FactoryStatus.PENDING),
        )
        self.assertIn(
            record.id,
            [r.id for r in await storage.list_active_factory_tickets()],
        )
        for status in (
            FactoryStatus.RUNNING,
            FactoryStatus.WAITING_HUMAN,
        ):
            record.data.status = status
            await storage.upsert_factory_ticket("user-1", record)
            self.assertIn(
                record.id,
                [r.id for r in await storage.list_active_factory_tickets()],
            )
        record.data.status = FactoryStatus.COMPLETED
        await storage.upsert_factory_ticket("user-1", record)
        self.assertEqual(
            [r.id for r in await storage.list_active_factory_tickets()],
            [],
        )


class RedisFactoryStorageTest(
    _FactoryStorageContractMixin,
    IsolatedAsyncioTestCase,
):
    """Upsert / get / list / delete over fakeredis."""

    async def _make_storage(self) -> StorageBase:
        return make_storage()

    async def test_active_index_self_heals(self) -> None:
        """Entries whose record vanished are pruned, not returned."""
        storage = make_storage()
        record = await storage.upsert_factory_ticket(
            "user-1",
            _factory_record(),
        )
        # Simulate the record key dying out from under the index
        # (expired TTL or an external delete).
        await storage._client.delete(
            storage._key(
                storage.key_config.factory_ticket,
                user_id="user-1",
                ticket_id=record.id,
            ),
        )
        self.assertEqual(
            await storage.list_active_factory_tickets(),
            [],
        )
        self.assertEqual(
            await storage._client.smembers(
                storage.key_config.factory_ticket_active_index,
            ),
            set(),
        )


class FactoryStatusColumnLengthTest(TestCase):
    """``factory_tickets.status`` is a ``String(32)`` column.

    SQLite silently ignores string lengths, but Postgres and MySQL
    enforce them — a longer enum value would only fail in production.
    """

    def test_status_values_fit_the_column(self) -> None:
        self.assertLessEqual(
            max(len(s.value) for s in FactoryStatus),
            32,
        )


class SqlFactoryStorageTest(
    _FactoryStorageContractMixin,
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
