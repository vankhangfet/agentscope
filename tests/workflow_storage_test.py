# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Workflow record persistence on both storage backends."""
from contextlib import AsyncExitStack
from datetime import datetime
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis

from agentscope.app.storage import (
    AgentNodeConfig,
    AsyncSQLAlchemyStorage,
    HumanApprovalNodeConfig,
    RedisStorage,
    StorageBase,
    WorkflowData,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRecord,
)


def make_storage() -> RedisStorage:
    """Create a RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def _workflow_record() -> WorkflowRecord:
    """A one-node workflow record."""
    return WorkflowRecord(
        user_id="user-1",
        data=WorkflowData(
            name="ship",
            description="do the thing",
            graph=WorkflowGraph(
                nodes=[
                    WorkflowNode(
                        id="a",
                        config=AgentNodeConfig(agent_id="agent-1"),
                    ),
                ],
            ),
        ),
    )


class _WorkflowStorageContractMixin:
    """Backend-agnostic workflow storage behaviours.

    Concrete classes pair this with
    :class:`~unittest.async_case.IsolatedAsyncioTestCase` and implement
    :meth:`_make_storage`, so every test below runs once per backend and
    keeps the Redis and SQL implementations honest about the same
    contract.
    """

    async def _make_storage(self) -> StorageBase:
        """Return the backend under test, ready for use."""
        raise NotImplementedError

    async def test_upsert_twice_is_idempotent_in_the_index(self) -> None:
        storage = await self._make_storage()
        record = _workflow_record()
        first = await storage.upsert_workflow("user-1", record)
        record.data.name = "ship v2"
        second = await storage.upsert_workflow("user-1", record)
        self.assertEqual(first.id, second.id)
        listed = await storage.list_workflows("user-1")
        self.assertEqual([r.id for r in listed], [second.id])
        self.assertEqual(listed[0].data.name, "ship v2")

    async def test_other_users_list_is_empty(self) -> None:
        storage = await self._make_storage()
        await storage.upsert_workflow("user-1", _workflow_record())
        self.assertEqual(await storage.list_workflows("user-2"), [])

    async def test_list_workflows_is_ordered_oldest_first(self) -> None:
        """Creation order wins regardless of insertion order: the
        records are written newest-first, so a backend that returns
        rows in storage order (Redis set members / SQL rowids) rather
        than sorting fails here."""
        storage = await self._make_storage()
        by_day: dict[int, str] = {}
        for day in (3, 1, 2):
            record = WorkflowRecord(
                user_id="user-1",
                created_at=datetime(2026, 1, day),
                data=WorkflowData(
                    name=f"wf-{day}",
                    graph=WorkflowGraph(
                        nodes=[
                            WorkflowNode(
                                id="a",
                                config=AgentNodeConfig(agent_id="agent-1"),
                            ),
                        ],
                    ),
                ),
            )
            by_day[day] = (await storage.upsert_workflow("user-1", record)).id
        listed = await storage.list_workflows("user-1")
        self.assertEqual(
            [r.id for r in listed],
            [by_day[1], by_day[2], by_day[3]],
        )

    async def test_branching_node_config_survives_the_round_trip(self) -> None:
        storage = await self._make_storage()
        record = WorkflowRecord(
            user_id="user-1",
            data=WorkflowData(
                name="branched",
                graph=WorkflowGraph(
                    nodes=[
                        WorkflowNode(
                            id="h",
                            config=HumanApprovalNodeConfig(
                                timeout_branch="escalate",
                            ),
                        ),
                    ],
                ),
            ),
        )
        saved = await storage.upsert_workflow("user-1", record)
        fetched = await storage.get_workflow("user-1", saved.id)
        cfg = fetched.data.graph.nodes[0].config
        self.assertIsInstance(cfg, HumanApprovalNodeConfig)
        self.assertEqual(cfg.timeout_branch, "escalate")


class RedisWorkflowStorageTest(
    _WorkflowStorageContractMixin,
    IsolatedAsyncioTestCase,
):
    """Upsert / get / list / delete over fakeredis."""

    async def _make_storage(self) -> StorageBase:
        return make_storage()

    async def test_workflow_round_trip(self) -> None:
        storage = await self._make_storage()
        record = _workflow_record()
        saved = await storage.upsert_workflow("user-1", record)
        self.assertEqual(saved.data.name, "ship")

        fetched = await storage.get_workflow("user-1", saved.id)
        self.assertEqual(fetched.data.graph.nodes[0].id, "a")

        listed = await storage.list_workflows("user-1")
        self.assertEqual([r.id for r in listed], [saved.id])

        other = await storage.get_workflow("user-2", saved.id)
        self.assertIsNone(other)

        self.assertTrue(await storage.delete_workflow("user-1", saved.id))
        self.assertFalse(await storage.delete_workflow("user-1", saved.id))
        self.assertEqual(await storage.list_workflows("user-1"), [])


class SqlWorkflowStorageTest(
    _WorkflowStorageContractMixin,
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

    async def test_workflow_round_trip(self) -> None:
        record = _workflow_record()
        saved = await self.storage.upsert_workflow("user-1", record)

        fetched = await self.storage.get_workflow("user-1", saved.id)
        self.assertEqual(fetched.data.name, "ship")

        listed = await self.storage.list_workflows("user-1")
        self.assertEqual(len(listed), 1)

        self.assertIsNone(
            await self.storage.get_workflow("user-2", saved.id),
        )

        self.assertTrue(
            await self.storage.delete_workflow("user-1", saved.id),
        )
        self.assertEqual(await self.storage.list_workflows("user-1"), [])
