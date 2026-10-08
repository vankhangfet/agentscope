# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Workflow record persistence on both storage backends.

The SQL half lands in Task 4; it is skipped here until then.
"""
import unittest
from contextlib import AsyncExitStack
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis

from agentscope.app.storage import (
    AgentNodeConfig,
    AsyncSQLAlchemyStorage,
    RedisStorage,
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


class RedisWorkflowStorageTest(IsolatedAsyncioTestCase):
    """Upsert / get / list / delete over fakeredis."""

    async def test_workflow_round_trip(self) -> None:
        storage = make_storage()
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


@unittest.skip("SQL backend lands in Task 4")
class SqlWorkflowStorageTest(IsolatedAsyncioTestCase):
    """Same behaviours over in-memory SQLite."""

    async def test_workflow_round_trip(self) -> None:
        self._stack = AsyncExitStack()
        storage = await self._stack.enter_async_context(
            AsyncSQLAlchemyStorage(
                "sqlite+aiosqlite:///:memory:",
                create_tables=True,
            ),
        )
        try:
            record = _workflow_record()
            saved = await storage.upsert_workflow("user-1", record)

            fetched = await storage.get_workflow("user-1", saved.id)
            self.assertEqual(fetched.data.name, "ship")

            listed = await storage.list_workflows("user-1")
            self.assertEqual(len(listed), 1)

            self.assertIsNone(
                await storage.get_workflow("user-2", saved.id),
            )

            self.assertTrue(
                await storage.delete_workflow("user-1", saved.id),
            )
            self.assertEqual(await storage.list_workflows("user-1"), [])
        finally:
            await self._stack.aclose()
