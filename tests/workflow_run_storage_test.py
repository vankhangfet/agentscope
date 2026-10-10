# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Workflow run record persistence on both storage backends."""
from contextlib import AsyncExitStack
from datetime import datetime
from unittest import IsolatedAsyncioTestCase, TestCase

import fakeredis.aioredis

from agentscope.app.storage import (
    AgentNodeConfig,
    AsyncSQLAlchemyStorage,
    RedisStorage,
    RunStatus,
    StorageBase,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRunData,
    WorkflowRunRecord,
)


def make_storage() -> RedisStorage:
    """Create a RedisStorage instance backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def _run_record(
    user_id: str = "user-1",
    workflow_id: str = "wf-1",
    status: RunStatus = RunStatus.RUNNING,
    created_at: datetime | None = None,
) -> WorkflowRunRecord:
    """A one-node run record."""
    record = WorkflowRunRecord(
        user_id=user_id,
        data=WorkflowRunData(
            workflow_id=workflow_id,
            workflow_version=1,
            workflow_name="pipeline",
            graph=WorkflowGraph(
                nodes=[
                    WorkflowNode(
                        id="a",
                        config=AgentNodeConfig(agent_id="ag-1"),
                    ),
                ],
            ),
            status=status,
        ),
    )
    if created_at is not None:
        record.created_at = created_at
    return record


class _WorkflowRunStorageContractMixin:
    """Backend-agnostic workflow-run storage behaviours.

    Concrete classes pair this with
    :class:`~unittest.async_case.IsolatedAsyncioTestCase` and implement
    :meth:`_make_storage`, so every test below runs once per backend and
    keeps the Redis and SQL implementations honest about the same
    contract.
    """

    async def _make_storage(self) -> StorageBase:
        """Return the backend under test, ready for use."""
        raise NotImplementedError

    async def test_run_round_trip(self) -> None:
        storage = await self._make_storage()
        saved = await storage.upsert_workflow_run("user-1", _run_record())
        fetched = await storage.get_workflow_run("user-1", saved.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.data.status, RunStatus.RUNNING)
        self.assertEqual(fetched.data.graph.nodes[0].id, "a")

    async def test_runs_list_newest_first(self) -> None:
        storage = await self._make_storage()
        days = (
            datetime(2026, 1, 3),
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
        )
        ids = []
        for day in days:
            saved = await storage.upsert_workflow_run(
                "user-1",
                _run_record(created_at=day),
            )
            ids.append(saved.id)
        # Insertion order (Jan 3, Jan 1, Jan 2) differs from creation
        # order, so only a real ``created_at`` sort can pass here.
        self.assertEqual(
            [r.id for r in await storage.list_workflow_runs("user-1")],
            [ids[0], ids[2], ids[1]],
        )

    async def test_runs_filter_by_workflow(self) -> None:
        storage = await self._make_storage()
        for wf in ("wf-1", "wf-1", "wf-2"):
            await storage.upsert_workflow_run(
                "user-1",
                _run_record(workflow_id=wf),
            )
        self.assertEqual(
            len(await storage.list_workflow_runs("user-1", "wf-1")),
            2,
        )

    async def test_runs_filter_by_status(self) -> None:
        storage = await self._make_storage()
        seeded = {
            status: await storage.upsert_workflow_run(
                "user-1",
                _run_record(status=status),
            )
            for status in (
                RunStatus.RUNNING,
                RunStatus.SUCCEEDED,
                RunStatus.WAITING_HUMAN,
            )
        }
        for status, saved in seeded.items():
            self.assertEqual(
                [
                    run.id
                    for run in await storage.list_workflow_runs(
                        "user-1",
                        status=status,
                    )
                ],
                [saved.id],
            )
        # No filter: every seeded run comes back.
        self.assertEqual(
            len(await storage.list_workflow_runs("user-1")),
            len(seeded),
        )

    async def test_active_index_tracks_lifecycle(self) -> None:
        storage = await self._make_storage()
        run = await storage.upsert_workflow_run("user-1", _run_record())
        active = await storage.list_active_workflow_runs()
        self.assertEqual([r.id for r in active], [run.id])
        run.data.status = RunStatus.SUCCEEDED
        await storage.upsert_workflow_run("user-1", run)
        self.assertEqual(
            await storage.list_active_workflow_runs(),
            [],
        )

    async def test_waiting_human_run_is_active(self) -> None:
        """The recovery sweep also resumes parked runs' timeouts, so
        WAITING_HUMAN counts as active just like RUNNING."""
        storage = await self._make_storage()
        run = await storage.upsert_workflow_run(
            "user-1",
            _run_record(status=RunStatus.WAITING_HUMAN),
        )
        self.assertEqual(
            [r.id for r in await storage.list_active_workflow_runs()],
            [run.id],
        )

    async def test_owner_scoping(self) -> None:
        storage = await self._make_storage()
        run = await storage.upsert_workflow_run("user-1", _run_record())
        self.assertIsNone(
            await storage.get_workflow_run("user-2", run.id),
        )
        self.assertEqual(
            await storage.list_workflow_runs("user-2"),
            [],
        )
        # Deleting as the wrong owner must neither remove the run nor
        # drop its active-index entry.
        self.assertFalse(
            await storage.delete_workflow_run("user-2", run.id),
        )

    async def test_delete(self) -> None:
        storage = await self._make_storage()
        run = await storage.upsert_workflow_run("user-1", _run_record())
        self.assertTrue(
            await storage.delete_workflow_run("user-1", run.id),
        )
        self.assertFalse(
            await storage.delete_workflow_run("user-1", run.id),
        )
        self.assertEqual(
            await storage.list_active_workflow_runs(),
            [],
        )


class RedisWorkflowRunStorageTest(
    _WorkflowRunStorageContractMixin,
    IsolatedAsyncioTestCase,
):
    """Upsert / get / list / delete over fakeredis."""

    async def _make_storage(self) -> StorageBase:
        return make_storage()

    async def test_active_index_prunes_terminal_entries(self) -> None:
        """An entry whose run has gone terminal (write divergence: the
        terminal upsert's ``srem`` was lost) is neither returned nor
        kept in the index."""
        storage = await self._make_storage()
        run = await storage.upsert_workflow_run("user-1", _run_record())
        run.data.status = RunStatus.SUCCEEDED
        await storage.upsert_workflow_run("user-1", run)
        # Simulate the divergence: the entry survived the removal.
        active_key = storage.key_config.workflow_run_active_index
        await storage._client.sadd(active_key, f"user-1:{run.id}")
        self.assertEqual(await storage.list_active_workflow_runs(), [])
        self.assertEqual(
            await storage._client.smembers(active_key),
            set(),
        )


class RunStatusColumnLengthTest(TestCase):
    """``workflow_runs.status`` is a ``String(32)`` column.

    SQLite silently ignores string lengths, but Postgres and MySQL
    enforce them — a longer enum value would only fail in production.
    """

    def test_status_values_fit_the_column(self) -> None:
        self.assertLessEqual(max(len(s.value) for s in RunStatus), 32)


class SqlWorkflowRunStorageTest(
    _WorkflowRunStorageContractMixin,
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
