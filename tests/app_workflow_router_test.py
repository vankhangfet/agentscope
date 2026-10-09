# -*- coding: utf-8 -*-
"""Workflow CRUD endpoints over a real app and fake backends."""
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

HEADERS = {"X-User-ID": "alice"}


def _fake_backends() -> tuple:
    """Build a fakeredis-backed storage and message bus."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _Storage(RedisStorage):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    class _Bus(RedisMessageBus):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    return _Storage(), _Bus()


def _graph(agent_id: str) -> dict:
    """A two-node chain over the given agent."""
    return {
        "nodes": [
            {"id": "a", "config": {"type": "agent", "agent_id": agent_id}},
            {"id": "b", "config": {"type": "agent", "agent_id": agent_id}},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "b"}],
    }


class WorkflowRouterTest(IsolatedAsyncioTestCase):
    """CRUD, version bumps and validation over HTTP."""

    def setUp(self) -> None:
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        self._client = self.enterContext(
            TestClient(
                create_app(
                    storage=storage,
                    message_bus=bus,
                    workspace_manager=LocalWorkspaceManager(workdir),
                    enable_index_worker=False,
                ),
            ),
        )
        self._agent_id = self._client.post(
            "/agent/",
            json={"name": "worker", "system_prompt": "hi"},
            headers=HEADERS,
        ).json()["agent_id"]

    def _data(self) -> dict:
        return {
            "name": "pipeline",
            "description": "two steps",
            "graph": _graph(self._agent_id),
        }

    def test_workflow_round_trips_through_the_api(self) -> None:
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        workflow_id = created.json()["workflow_id"]

        fetched = self._client.get(
            f"/workflow/{workflow_id}",
            headers=HEADERS,
        )
        self.assertEqual(fetched.json()["data"]["name"], "pipeline")
        self.assertEqual(fetched.json()["data"]["version"], 1)

        listed = self._client.get("/workflow/", headers=HEADERS).json()
        self.assertEqual(listed["total"], 1)

        renamed = dict(self._data(), name="pipeline v2")
        patched = self._client.patch(
            f"/workflow/{workflow_id}",
            json={"data": renamed},
            headers=HEADERS,
        )
        self.assertEqual(patched.json()["data"]["name"], "pipeline v2")
        self.assertEqual(patched.json()["data"]["version"], 2)

        self.assertEqual(
            self._client.delete(
                f"/workflow/{workflow_id}",
                headers=HEADERS,
            ).status_code,
            204,
        )
        self.assertEqual(
            self._client.get(
                f"/workflow/{workflow_id}",
                headers=HEADERS,
            ).status_code,
            404,
        )

    def test_a_cyclic_graph_is_rejected_with_422(self) -> None:
        data = dict(
            self._data(),
            graph={
                "nodes": _graph(self._agent_id)["nodes"],
                "edges": [
                    {"id": "e1", "source": "a", "target": "b"},
                    {"id": "e2", "source": "b", "target": "a"},
                ],
            },
        )
        created = self._client.post(
            "/workflow/",
            json={"data": data},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)
        self.assertTrue(
            any(
                "Cycle detected" in p
                for p in created.json()["detail"]["problems"]
            ),
        )

    def test_unknown_agent_reference_is_rejected(self) -> None:
        data = dict(self._data(), graph=_graph("ghost"))
        created = self._client.post(
            "/workflow/",
            json={"data": data},
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)
        self.assertTrue(
            any(
                "unknown agent" in p
                for p in created.json()["detail"]["problems"]
            ),
        )

    def test_workflows_are_scoped_per_user(self) -> None:
        created = self._client.post(
            "/workflow/",
            json={"data": self._data()},
            headers=HEADERS,
        ).json()
        other = {"X-User-ID": "bob"}
        self.assertEqual(
            self._client.get(
                f"/workflow/{created['workflow_id']}",
                headers=other,
            ).status_code,
            404,
        )
        listed = self._client.get("/workflow/", headers=other).json()
        self.assertEqual(listed["total"], 0)
