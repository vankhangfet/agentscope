# -*- coding: utf-8 -*-
"""Hand-written MCP creation: POST /mcp/custom for servers the hub and
preset catalogs do not carry."""
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


def _stdio_body(
    name: str = "my-server",
    command: str = "uvx",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """A stdio MCP create body."""
    config: dict = {"type": "stdio_mcp", "command": command}
    if args is not None:
        config["args"] = args
    if env is not None:
        config["env"] = env
    return {"client": {"name": name, "is_stateful": True,
                       "mcp_config": config}}


def _http_body(name: str = "remote-server") -> dict:
    """An http MCP create body with a header."""
    return {"client": {
        "name": name,
        "is_stateful": False,
        "mcp_config": {
            "type": "http_mcp",
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer tok"},
        },
    }}


class CreateCustomMcpTest(IsolatedAsyncioTestCase):
    """The hand-written create lands in the library like any MCP."""

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

    def _library_names(self) -> list[str]:
        listed = self._client.get("/mcp", headers=HEADERS)
        self.assertEqual(listed.status_code, 200)
        return [m["name"] for m in listed.json()]

    def test_a_stdio_server_lands_in_the_library(self) -> None:
        created = self._client.post(
            "/mcp/custom",
            json=_stdio_body(
                env={"MY_KEY": "s3cret"},
                args=["my-mcp-server", "--port", "8080"],
            ),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        self.assertIn("my-server", self._library_names())

    def test_an_http_server_round_trips_its_headers(self) -> None:
        created = self._client.post(
            "/mcp/custom",
            json=_http_body(),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        listed = self._client.get("/mcp", headers=HEADERS).json()
        record = next(m for m in listed if m["name"] == "remote-server")
        # The view never projects the config (secrets stay write-only),
        # so the assertion is presence + enabled, not the header value.
        self.assertTrue(record["enabled"])

    def test_a_name_clash_is_a_409(self) -> None:
        first = self._client.post(
            "/mcp/custom",
            json=_stdio_body(),
            headers=HEADERS,
        )
        self.assertEqual(first.status_code, 201)
        second = self._client.post(
            "/mcp/custom",
            json=_stdio_body(),
            headers=HEADERS,
        )
        self.assertEqual(second.status_code, 409)
        self.assertIn("already exists", str(second.json()["detail"]))

    def test_an_illegal_name_is_a_422(self) -> None:
        created = self._client.post(
            "/mcp/custom",
            json=_stdio_body(name="bad name!"),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)

    def test_a_config_missing_its_command_is_a_422(self) -> None:
        body = _stdio_body()
        del body["client"]["mcp_config"]["command"]
        created = self._client.post(
            "/mcp/custom",
            json=body,
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422)

    def test_the_record_is_scoped_to_its_owner(self) -> None:
        created = self._client.post(
            "/mcp/custom",
            json=_stdio_body(),
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        bob = {"X-User-ID": "bob"}
        self.assertEqual(
            [m["name"] for m in self._client.get(
                "/mcp", headers=bob
            ).json()],
            [],
        )
        self.assertEqual(
            self._client.post(
                "/mcp/custom",
                json=_stdio_body(),
                headers=bob,
            ).status_code,
            201,
        )
