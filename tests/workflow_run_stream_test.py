# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Run-event SSE endpoint: state snapshot, live events, clean teardown.

The live path drives the real app — router, engine, chat service,
fake model — on the test's own loop via ``httpx.ASGITransport`` (the
schedule E2E scaffold), because a run's stream only ends at its
terminal event and the buffering test transports expose the body
exactly once the response completes. Teardown — the bus subscription
released on close and on disconnect — is pinned by calling the route
coroutine directly against an ``InMemoryMessageBus``, mirroring the
session-stream seam test.
"""
import asyncio
import json
import tempfile
import time
from contextlib import AsyncExitStack, aclosing
from typing import Any
from unittest import IsolatedAsyncioTestCase

import httpx

from app_workflow_run_router_test import HEADERS, _fake_backends, _graph

from agentscope.app import create_app
from agentscope.app._lifespan import lifespan
from agentscope.app._router._workflow import stream_run_events
from agentscope.app._router._schema import RunDetailResponse
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    AgentNodeConfig,
    RunStatus,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRunData,
    WorkflowRunRecord,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _line_graph(agent_id: str) -> dict:
    """A single-agent-node workflow that runs straight to SUCCEEDED."""
    return {
        "nodes": [
            {"id": "only", "config": {"type": "agent", "agent_id": agent_id}},
        ],
        "edges": [],
    }


def _parse_sse(text: str) -> list[tuple[str | None, dict]]:
    """Decode every ``(event, data)`` frame in one SSE body."""
    frames: list[tuple[str | None, dict]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            frames.append((event, json.loads(line[len("data: ") :])))
    return frames


def _record(status: RunStatus) -> WorkflowRunRecord:
    """A minimal run record for the direct route-call tests."""
    return WorkflowRunRecord(
        user_id="u-1",
        data=WorkflowRunData(
            workflow_id="wf-1",
            workflow_version=1,
            workflow_name="gate",
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


class _StaticRunService:
    """Duck-typed WorkflowService returning one fixed record."""

    def __init__(self, record: WorkflowRunRecord) -> None:
        self._record = record

    async def get_run(
        self,
        user_id: str,
        run_id: str,
    ) -> WorkflowRunRecord:
        """Return the seeded record whatever id was asked for."""
        del user_id, run_id
        return self._record


class RunStreamE2ETest(IsolatedAsyncioTestCase):
    """Stream real runs over the real app on this test's loop."""

    async def _app(self) -> tuple[AsyncExitStack, httpx.AsyncClient, Any]:
        """A booted app and client; torn down with the test."""
        stack = AsyncExitStack()
        self.addAsyncCleanup(stack.aclose)
        workdir = stack.enter_context(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        app = create_app(
            storage=storage,
            message_bus=bus,
            workspace_manager=LocalWorkspaceManager(workdir),
            enable_index_worker=False,
            enable_scheduler=False,
        )
        await stack.enter_async_context(lifespan(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers=HEADERS,
                timeout=30.0,
            ),
        )
        return stack, client, bus

    async def _seed(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """Create the fake credential and the worker agent."""
        created = await client.post(
            "/credential/",
            json={"data": {"type": "fake_router_credential", "name": "fake"}},
        )
        self.assertEqual(created.status_code, 201)
        credential_id = created.json()["credential_id"]
        agent_id = (
            await client.post(
                "/agent/",
                json={"name": "worker", "system_prompt": "hi"},
            )
        ).json()["agent_id"]
        return credential_id, agent_id

    async def _start(
        self,
        client: httpx.AsyncClient,
        graph: dict,
        credential_id: str,
    ) -> str:
        """Create a workflow from *graph* and start a run of it."""
        created = await client.post(
            "/workflow/",
            json={"data": {"name": "streamed", "graph": graph}},
        )
        self.assertEqual(created.status_code, 201)
        started = await client.post(
            f"/workflow/{created.json()['workflow_id']}/run",
            json={
                "input": {"topic": "tests"},
                "chat_model_config": {
                    "type": "fake",
                    "credential_id": credential_id,
                    "model": "ok-model",
                    "parameters": {},
                },
            },
        )
        self.assertEqual(started.status_code, 201)
        return started.json()["run_id"]

    async def _poll_status(
        self,
        client: httpx.AsyncClient,
        run_id: str,
        statuses: set[str],
    ) -> dict:
        """Poll one run until it reaches one of *statuses*."""
        deadline = time.monotonic() + 10.0
        data: dict | None = None
        while time.monotonic() < deadline:
            fetched = await client.get(f"/workflow/runs/{run_id}")
            data = fetched.json()["data"]
            if data["status"] in statuses:
                return data
            await asyncio.sleep(0.05)
        self.fail(
            f"Run {run_id!r} never reached {statuses}; "
            f"last status {data and data['status']!r}.",
        )

    async def _subscribers(self, bus: Any, channel: str) -> int:
        """How many bus subscribers currently hold *channel*."""
        counts = dict(await bus._client.pubsub_numsub(channel))
        return counts.get(channel, 0)

    async def _wait_subscribers(
        self,
        bus: Any,
        channel: str,
        count: int,
    ) -> None:
        """Block until *channel* has exactly *count* subscribers."""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if await self._subscribers(bus, channel) == count:
                return
            await asyncio.sleep(0.02)
        self.fail(f"{channel!r} never settled on {count} subscriber(s).")

    async def _read_stream(
        self,
        client: httpx.AsyncClient,
        run_id: str,
    ) -> tuple[int, str, str]:
        """Consume one whole stream response: status, type, body."""
        async with client.stream(
            "GET",
            f"/workflow/runs/{run_id}/stream",
        ) as response:
            body = (await response.aread()).decode()
            return (
                response.status_code,
                response.headers.get("content-type", ""),
                body,
            )

    # --------------------------------------------------------------- tests

    async def test_unknown_run_is_404(self) -> None:
        """Streaming an unknown run answers 404, not a broken stream."""
        _, client, _ = await self._app()
        response = await client.get("/workflow/runs/run-missing/stream")
        self.assertEqual(response.status_code, 404)

    async def test_terminal_run_emits_state_then_closes(self) -> None:
        """An already-finished run yields one state frame and ends."""
        _, client, _ = await self._app()
        credential_id, agent_id = await self._seed(client)
        run_id = await self._start(
            client,
            _line_graph(agent_id),
            credential_id,
        )
        done = await self._poll_status(client, run_id, _TERMINAL)
        self.assertEqual(done["status"], "SUCCEEDED")

        status_code, content_type, body = await self._read_stream(
            client,
            run_id,
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(content_type.startswith("text/event-stream"))
        self.assertTrue(body.startswith("event: state\n"))
        frames = _parse_sse(body)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], "state")
        self.assertEqual(frames[0][1]["data"]["status"], "SUCCEEDED")
        # Subscription release on close is pinned by the teardown
        # tests below: fakeredis's ``pubsub_numsub`` only decrements
        # on explicit ``unsubscribe``, not on the pubsub ``aclose``
        # the Redis bus relies on (a real server drops the
        # subscription with the connection).

    async def test_live_run_streams_events_until_terminal(self) -> None:
        """A parked run streams state, then node and run events."""
        _, client, bus = await self._app()
        credential_id, agent_id = await self._seed(client)
        run_id = await self._start(client, _graph(agent_id), credential_id)
        await self._poll_status(client, run_id, {"WAITING_HUMAN"})

        channel = f"workflow_run:{run_id}"
        streamed = asyncio.create_task(self._read_stream(client, run_id))
        # Approve only once the stream's subscription is live, so no
        # event can be missed to the subscribe/publish seam.
        await self._wait_subscribers(bus, channel, 1)
        approved = await client.post(
            f"/workflow/runs/{run_id}/approve",
            json={"node_id": "gate", "verdict": "approve", "note": "go"},
        )
        self.assertEqual(approved.status_code, 200)

        status_code, content_type, body = await asyncio.wait_for(
            streamed,
            timeout=30.0,
        )
        self.assertEqual(status_code, 200)
        self.assertTrue(content_type.startswith("text/event-stream"))
        frames = _parse_sse(body)
        self.assertEqual(frames[0][0], "state")
        self.assertEqual(frames[0][1]["data"]["status"], "WAITING_HUMAN")
        events = [
            (event, data.get("node_id"), data.get("status"))
            for event, data in frames[1:]
        ]
        self.assertIn(("node", "gate", "SUCCEEDED"), events)
        self.assertIn(("node", "ok", "SUCCEEDED"), events)
        self.assertEqual(events[-1], ("run", None, "SUCCEEDED"))
        for _, data in frames[1:]:
            self.assertEqual(data["run_id"], run_id)


class RunStreamTeardownTest(IsolatedAsyncioTestCase):
    """The stream releases its bus subscription on close/disconnect."""

    async def test_terminal_run_emits_state_and_unsubscribes(self) -> None:
        """A terminal snapshot is one frame; the feeder then unwinds."""
        bus = InMemoryMessageBus()
        record = _record(RunStatus.SUCCEEDED)
        response = await stream_run_events(
            run_id="run-1",
            user_id="u-1",
            service=_StaticRunService(record),  # type: ignore[arg-type]
            message_bus=bus,
        )
        frames: list[str] = []
        async with aclosing(response.body_iterator) as stream:
            async for frame in stream:
                frames.append(frame)
        expected = (
            "event: state\n"
            f"data: {RunDetailResponse(data=record.data).model_dump_json()}"
            "\n\n"
        )
        self.assertEqual(frames, [expected])
        self.assertEqual(
            bus._subscribers.get("workflow_run:run-1", set()),
            set(),
        )

    async def test_parked_stream_forwards_events_until_disconnect(
        self,
    ) -> None:
        """Live events flow while open; closing drops the subscription."""
        bus = InMemoryMessageBus()
        record = _record(RunStatus.WAITING_HUMAN)
        channel = "workflow_run:run-1"
        response = await stream_run_events(
            run_id="run-1",
            user_id="u-1",
            service=_StaticRunService(record),  # type: ignore[arg-type]
            message_bus=bus,
        )
        payload = {
            "type": "node",
            "run_id": "run-1",
            "node_id": "gate",
            "status": "SUCCEEDED",
            "error": None,
        }
        async with aclosing(response.body_iterator) as stream:
            state = await anext(stream)
            self.assertTrue(state.startswith("event: state\n"))
            self.assertEqual(len(bus._subscribers[channel]), 1)
            await bus.publish(channel, payload)
            live = await asyncio.wait_for(anext(stream), timeout=5.0)
        self.assertEqual(
            live,
            "event: node\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n",
        )
        # Leaving the block closed the connection (client disconnect):
        # the generator's finally cancelled the feeder, whose own
        # finally discarded the bus subscription.
        self.assertEqual(bus._subscribers.get(channel, set()), set())
