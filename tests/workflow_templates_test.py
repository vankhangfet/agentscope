# -*- coding: utf-8 -*-
"""The built-in workflow template catalog — data and endpoint.

Templates are the no-blank-canvas path into workflows: shipped graphs
whose agent and channel references are empty *slots*, listed without
auth, and installed by mapping the slots before the strict create.
"""
import copy
import re
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app._service import (
    TEMPLATE_CATEGORIES,
    TEMPLATES,
    template_view,
)
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import (
    AgentNodeConfig,
    ChannelBinding,
    ChannelRecord,
    NotifyNodeConfig,
    RedisStorage,
    RoutingConfig,
    SessionSettings,
    WorkflowGraph,
    validate_workflow_graph,
)
from agentscope.app.workspace_manager import LocalWorkspaceManager

HEADERS = {"X-User-ID": "alice"}

EXPECTED_IDS = {
    "channel-notification",
    "product-design",
    "news-digest",
    "vietcombank-fx",
}

_TAG = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")


class TemplateCatalogTest(TestCase):
    """The shipped catalog holds four coherent, slot-honest graphs."""

    def test_lists_the_four_designed_templates(self) -> None:
        self.assertEqual(len(TEMPLATES), 4)
        self.assertEqual({t.id for t in TEMPLATES}, EXPECTED_IDS)
        for template in TEMPLATES:
            self.assertTrue(template.name, template.id)
            self.assertTrue(template.description, template.id)
            self.assertIn(template.category, TEMPLATE_CATEGORIES)

    def test_categories_match_the_design(self) -> None:
        by_id = {t.id: t for t in TEMPLATES}
        self.assertEqual(
            by_id["channel-notification"].category,
            "Channels",
        )
        self.assertEqual(by_id["product-design"].category, "Design")
        self.assertEqual(by_id["news-digest"].category, "Digest")
        self.assertEqual(by_id["vietcombank-fx"].category, "Digest")

    def test_every_graph_passes_structural_validation(self) -> None:
        for template in TEMPLATES:
            self.assertEqual(
                validate_workflow_graph(template.graph),
                [],
                template.id,
            )

    def test_agent_slots_mirror_the_agent_nodes(self) -> None:
        for template in TEMPLATES:
            agent_nodes = [
                node
                for node in template.graph.nodes
                if isinstance(node.config, AgentNodeConfig)
            ]
            self.assertEqual(
                [slot.node_id for slot in template.agent_slots],
                [node.id for node in agent_nodes],
                template.id,
            )
            for node in agent_nodes:
                self.assertEqual(node.config.agent_id, "", node.id)

    def test_channel_slots_mirror_the_notify_nodes(self) -> None:
        for template in TEMPLATES:
            notify_nodes = [
                node
                for node in template.graph.nodes
                if isinstance(node.config, NotifyNodeConfig)
            ]
            self.assertTrue(notify_nodes, template.id)
            self.assertEqual(
                [slot.node_id for slot in template.channel_slots],
                [node.id for node in notify_nodes],
                template.id,
            )
            for node in notify_nodes:
                self.assertEqual(node.config.channel_id, "", node.id)

    def test_slot_labels_describe_the_job(self) -> None:
        for template in TEMPLATES:
            for slot in (*template.agent_slots, *template.channel_slots):
                self.assertTrue(slot.label.strip(), (template.id, slot))

    def test_node_references_inside_templates_resolve(self) -> None:
        """Every ``{{nodes.<id>...}}`` tag names a node the graph
        holds — a typo'd reference would fail the run, not the save."""
        for template in TEMPLATES:
            ids = {node.id for node in template.graph.nodes}
            for node in template.graph.nodes:
                cfg = node.config
                if isinstance(cfg, AgentNodeConfig):
                    text = cfg.prompt_template
                elif isinstance(cfg, NotifyNodeConfig):
                    text = cfg.message_template
                else:
                    continue
                for tag in _TAG.findall(text):
                    parts = tag.split(".")
                    if parts[0] == "nodes":
                        self.assertIn(parts[1], ids, (template.id, tag))

    def test_view_projects_the_catalog_entry(self) -> None:
        for template in TEMPLATES:
            view = template_view(template)
            self.assertEqual(
                set(view),
                {
                    "id",
                    "name",
                    "description",
                    "category",
                    "graph",
                    "agent_slots",
                    "channel_slots",
                },
            )
            for slot in (*view["agent_slots"], *view["channel_slots"]):
                self.assertEqual(set(slot), {"node_id", "label", "hint"})
            graph = WorkflowGraph.model_validate(view["graph"])
            self.assertEqual(
                [node.id for node in graph.nodes],
                [node.id for node in template.graph.nodes],
            )


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


async def _seed_channel(
    storage: RedisStorage,
    channel_id: str,
    user_id: str,
) -> None:
    """Insert one enabled channel record, bypassing the channel API."""
    record = ChannelRecord(
        id=channel_id,
        channel_type="wiring-fake",
        name="Ops room",
        user_id=user_id,
        enabled=True,
        credentials={"bot_id": f"bot-{channel_id}"},
        routing=RoutingConfig(
            bindings=[ChannelBinding(match_value="*", agent_id="agent-x")],
        ),
        session=SessionSettings(chat_model_config={"type": "x"}),
    )
    await storage.upsert_channel(
        record,
        platform_bot_id=f"bot-{channel_id}",
    )


def _mapped(graph: dict, agent_id: str, channel_id: str) -> dict:
    """Fill a template's slots, as the install dialog would."""
    mapped = copy.deepcopy(graph)
    for node in mapped["nodes"]:
        cfg = node["config"]
        if cfg["type"] == "agent":
            cfg["agent_id"] = agent_id
        elif cfg["type"] == "notify":
            cfg["channel_id"] = channel_id
    return mapped


class TemplateEndpointTest(IsolatedAsyncioTestCase):
    """GET /workflow/templates: static catalog, no auth, honest slots."""

    async def asyncSetUp(self) -> None:
        self._tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage, bus = _fake_backends()
        # Seeded before the app boots so only this test's event loop
        # touches fakeredis while the record is written.
        # pylint: disable-next=unnecessary-dunder-call
        await self.storage.__aenter__()
        await _seed_channel(self.storage, "ch-ok", "alice")
        self.app = create_app(
            storage=self.storage,
            message_bus=bus,
            workspace_manager=LocalWorkspaceManager(self._tmp),
            enable_index_worker=False,
        )
        self._client = self.enterContext(TestClient(self.app))
        self._agent_id = self._client.post(
            "/agent/",
            json={"name": "worker", "system_prompt": "hi"},
            headers=HEADERS,
        ).json()["agent_id"]

    def _templates(self) -> list[dict]:
        """The catalog over HTTP."""
        response = self._client.get("/workflow/templates")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["templates"]

    def _template(self, template_id: str) -> dict:
        return next(
            t for t in self._templates() if t["id"] == template_id
        )

    def test_lists_templates_without_an_auth_header(self) -> None:
        """No X-User-ID needed — and had the literal segment been
        captured by ``/{workflow_id}``, the same call would 401."""
        response = self._client.get("/workflow/templates")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 4)
        self.assertEqual({t["id"] for t in body["templates"]}, EXPECTED_IDS)

    def test_route_precedes_the_workflow_id_routes(self) -> None:
        """The literal segment is declared before the parameterized
        one, in the workflow router's own declaration order."""
        workflow_routes = next(
            included.original_router.routes
            for included in self.app.routes
            if not hasattr(included, "path")
            and included.original_router.prefix == "/workflow"
        )
        paths = [route.path for route in workflow_routes]
        self.assertLess(
            paths.index("/workflow/templates"),
            paths.index("/workflow/{workflow_id}"),
        )

    def test_listed_graphs_round_trip_into_the_model(self) -> None:
        by_id = {t.id: t for t in TEMPLATES}
        for view in self._templates():
            graph = WorkflowGraph.model_validate(view["graph"])
            self.assertEqual(
                len(graph.nodes),
                len(by_id[view["id"]].graph.nodes),
            )

    def test_slot_contract_survives_serialization(self) -> None:
        for view in self._templates():
            for node in view["graph"]["nodes"]:
                cfg = node["config"]
                if cfg["type"] == "agent":
                    self.assertEqual(cfg["agent_id"], "")
                elif cfg["type"] == "notify":
                    self.assertEqual(cfg["channel_id"], "")

    def test_unmapped_template_save_is_rejected_naming_the_nodes(
        self,
    ) -> None:
        """The strict-save contract: a template installed as-is, slots
        still empty, is a 422 naming every node that needs mapping."""
        view = self._template("product-design")
        created = self._client.post(
            "/workflow/",
            json={
                "data": {
                    "name": view["name"],
                    "graph": view["graph"],
                },
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 422, created.text)
        problems = created.json()["detail"]["problems"]
        self.assertTrue(
            any(
                "needs an agent" in p and "write_brief" in p
                for p in problems
            ),
            problems,
        )
        self.assertTrue(
            any(
                "needs a channel" in p and "notify_result" in p
                for p in problems
            ),
            problems,
        )
        for node in view["graph"]["nodes"]:
            if node["config"]["type"] in ("agent", "notify"):
                self.assertTrue(
                    any(node["id"] in p for p in problems),
                    (node["id"], problems),
                )

    def test_mapped_template_creates_a_workflow(self) -> None:
        """The install's happy path: slots mapped, the shipped graph
        saves as-is through the ordinary create."""
        view = self._template("product-design")
        created = self._client.post(
            "/workflow/",
            json={
                "data": {
                    "name": view["name"],
                    "graph": _mapped(
                        view["graph"],
                        self._agent_id,
                        "ch-ok",
                    ),
                },
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201, created.text)
        fetched = self._client.get(
            f"/workflow/{created.json()['workflow_id']}",
            headers=HEADERS,
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["data"]["name"], view["name"])
        self.assertEqual(
            len(fetched.json()["data"]["graph"]["nodes"]),
            len(view["graph"]["nodes"]),
        )
