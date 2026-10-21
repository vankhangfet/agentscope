# -*- coding: utf-8 -*-
"""The built-in workflow templates — a catalog of starting points.

Each template is a complete graph whose agent and channel references
are deliberately empty: those are *slots*, mapped to the user's real
records in the install dialog before the create. Saves stay strict —
an empty ``agent_id``/``channel_id`` is a save-blocked problem in the
service — so this catalog is the only place empty references
legitimately live.

Kept as plain frozen data, like the integration presets, so the
endpoint can list it with no auth and no storage. The import-time
check below keeps the shipped graphs honest — structure, slot
coverage, in-template references — so a broken template fails at
import, loudly, rather than shipping a graph no strict save accepts.
"""
from dataclasses import dataclass

from ..storage import (
    AgentNodeConfig,
    HumanApprovalNodeConfig,
    NotifyNodeConfig,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPosition,
    validate_workflow_graph,
)
from ._workflow_template import _TAG

TEMPLATE_CATEGORIES: tuple[str, ...] = ("Channels", "Design", "Digest")
"""The categories the gallery groups templates by."""


@dataclass(frozen=True)
class TemplateSlot:
    """One reference the install dialog must map before creating."""

    node_id: str
    """The node whose config carries the empty reference."""

    label: str
    """What the mapped record is for — the agent's job, or the
    channel's role in the flow."""

    hint: str = ""
    """Extra guidance shown under the label."""


@dataclass(frozen=True)
class WorkflowTemplate:
    """One catalog entry: metadata, its graph, and its slots."""

    id: str
    name: str
    description: str
    category: str
    graph: WorkflowGraph
    agent_slots: tuple[TemplateSlot, ...]
    channel_slots: tuple[TemplateSlot, ...]


# ----------------------------------------------------------------------
# Node and edge builders — the catalog reads as data, not as pydantic
# constructor calls.
# ----------------------------------------------------------------------


def _agent(node_id: str, x: float, y: float, prompt: str) -> WorkflowNode:
    """An agent node with its reference left empty (a slot)."""
    return WorkflowNode(
        id=node_id,
        position=WorkflowPosition(x=x, y=y),
        config=AgentNodeConfig(agent_id="", prompt_template=prompt),
    )


def _approval(node_id: str, x: float, y: float) -> WorkflowNode:
    """A human approval gate with the default owner and timeout."""
    return WorkflowNode(
        id=node_id,
        position=WorkflowPosition(x=x, y=y),
        config=HumanApprovalNodeConfig(),
    )


def _notify(node_id: str, x: float, y: float, message: str) -> WorkflowNode:
    """A notify node with its reference left empty (a slot)."""
    return WorkflowNode(
        id=node_id,
        position=WorkflowPosition(x=x, y=y),
        config=NotifyNodeConfig(channel_id="", message_template=message),
    )


def _edge(
    edge_id: str,
    source: str,
    target: str,
    branch: str | None = None,
) -> WorkflowEdge:
    """One directed connection; ``branch`` labels an approval arm."""
    return WorkflowEdge(
        id=edge_id,
        source=source,
        target=target,
        branch=branch,
    )


def _graph(
    *nodes: WorkflowNode,
    edges: tuple[WorkflowEdge, ...] = (),
) -> WorkflowGraph:
    """Assemble nodes and edges into a graph."""
    return WorkflowGraph(nodes=list(nodes), edges=list(edges))


# ----------------------------------------------------------------------
# Prompts and messages — domain-neutral, instructive, with the run's
# input and upstream outputs spliced in as blackboard paths.
# ----------------------------------------------------------------------

_BRIEF_PROMPT = (
    "Research the product idea in {{input.idea}} using your tools, "
    "then write a short product brief: the problem, the target user, "
    "and the three must-have capabilities. Reply with the brief only."
)

_PROPOSE_PROMPT = (
    "Product brief:\n"
    "{{nodes.write_brief.output.text}}\n\n"
    "Propose a solution design for it: the components, what each is "
    "responsible for, and how data flows between them. Keep it to "
    "one page."
)

_REFINE_PROMPT = (
    "A reviewer approved this design:\n"
    "{{nodes.propose_design.output.text}}\n\n"
    "Refine it into the final specification: resolve the open "
    "questions and cover error handling and edge cases. Reply with "
    "the specification only."
)

_DESIGN_RESULT_MESSAGE = (
    "Design approved and refined:\n\n"
    "{{nodes.refine_design.output.text}}"
)

_DESIGN_REJECTED_MESSAGE = (
    "Design rejected at review. The proposal was:\n\n"
    "{{nodes.propose_design.output.text}}"
)

_DIGEST_PROMPT = (
    "Research today's news on the topic {{input.topic}}. Use your "
    "shell or MCP tools to collect a handful of recent items from "
    "reliable sources, then summarize them as a bullet list — one "
    "bullet per item, each ending with its source name. Reply with "
    "the bullet list only."
)

_DIGEST_MESSAGE = (
    "News digest — {{input.topic}}:\n\n"
    "{{nodes.digest_news.output.text}}"
)

_DIGEST_REJECTED_MESSAGE = (
    "The news digest on {{input.topic}} was rejected at review."
)

_FX_PROMPT = (
    "Fetch Vietcombank's current exchange rates. Use the shell tool "
    "(PowerShell Invoke-RestMethod, or curl) to GET the bank's public "
    "exchange-rate endpoint at https://www.vietcombank.com.vn, parse "
    "the buy and sell rates for the major currencies (USD, EUR, JPY "
    "and the rest), and reply with a compact markdown table as plain "
    "text — one row per currency."
)

_FX_MESSAGE = (
    "Vietcombank FX rates:\n\n"
    "{{nodes.fetch_rates.output.text}}"
)


# ----------------------------------------------------------------------
# The catalog.
# ----------------------------------------------------------------------

TEMPLATES: tuple[WorkflowTemplate, ...] = (
    WorkflowTemplate(
        id="channel-notification",
        name="Channel notification",
        description=(
            "Send the run's input message straight to a channel — "
            "the smallest possible notify workflow."
        ),
        category="Channels",
        graph=_graph(
            _notify("notify_message", 0.0, 0.0, "{{input.message}}"),
        ),
        agent_slots=(),
        channel_slots=(
            TemplateSlot(
                node_id="notify_message",
                label=(
                    "Notify channel — receives the run's input message"
                ),
                hint=(
                    "Run input: {\"message\": \"...\"} — whatever "
                    "input.message holds is sent as-is."
                ),
            ),
        ),
    ),
    WorkflowTemplate(
        id="product-design",
        name="Product design review",
        description=(
            "A brief agent researches the idea, a designer proposes, "
            "a human approves, and the refined design is sent to a "
            "channel."
        ),
        category="Design",
        graph=_graph(
            _agent("write_brief", 0.0, 0.0, _BRIEF_PROMPT),
            _agent("propose_design", 220.0, 0.0, _PROPOSE_PROMPT),
            _approval("review_design", 440.0, 0.0),
            _agent("refine_design", 660.0, 0.0, _REFINE_PROMPT),
            _notify(
                "notify_result",
                880.0,
                0.0,
                _DESIGN_RESULT_MESSAGE,
            ),
            _notify(
                "notify_rejected",
                660.0,
                160.0,
                _DESIGN_REJECTED_MESSAGE,
            ),
            edges=(
                _edge("e_brief_propose", "write_brief", "propose_design"),
                _edge(
                    "e_propose_review",
                    "propose_design",
                    "review_design",
                ),
                _edge(
                    "e_review_refine",
                    "review_design",
                    "refine_design",
                    branch="approve",
                ),
                _edge(
                    "e_review_rejected",
                    "review_design",
                    "notify_rejected",
                    branch="reject",
                ),
                _edge(
                    "e_refine_result",
                    "refine_design",
                    "notify_result",
                ),
            ),
        ),
        agent_slots=(
            TemplateSlot(
                node_id="write_brief",
                label=(
                    "Brief writer — researches the product idea and "
                    "writes the brief"
                ),
            ),
            TemplateSlot(
                node_id="propose_design",
                label=(
                    "Designer — turns the brief into a solution design"
                ),
            ),
            TemplateSlot(
                node_id="refine_design",
                label=(
                    "Refiner — folds the approved design into a final "
                    "specification"
                ),
            ),
        ),
        channel_slots=(
            TemplateSlot(
                node_id="notify_result",
                label="Result channel — receives the refined design",
            ),
            TemplateSlot(
                node_id="notify_rejected",
                label="Rejection channel — receives the rejected proposal",
            ),
        ),
    ),
    WorkflowTemplate(
        id="news-digest",
        name="News digest",
        description=(
            "An agent digests today's news on a topic, a human "
            "approves, and the digest goes to a channel."
        ),
        category="Digest",
        graph=_graph(
            _agent("digest_news", 0.0, 0.0, _DIGEST_PROMPT),
            _approval("review_digest", 220.0, 0.0),
            _notify("notify_digest", 440.0, 0.0, _DIGEST_MESSAGE),
            _notify(
                "notify_rejected",
                440.0,
                160.0,
                _DIGEST_REJECTED_MESSAGE,
            ),
            edges=(
                _edge("e_digest_review", "digest_news", "review_digest"),
                _edge(
                    "e_review_digest",
                    "review_digest",
                    "notify_digest",
                    branch="approve",
                ),
                _edge(
                    "e_review_rejected",
                    "review_digest",
                    "notify_rejected",
                    branch="reject",
                ),
            ),
        ),
        agent_slots=(
            TemplateSlot(
                node_id="digest_news",
                label=(
                    "Digest writer — collects news on the topic and "
                    "summarizes it as bullets"
                ),
            ),
        ),
        channel_slots=(
            TemplateSlot(
                node_id="notify_digest",
                label="Digest channel — receives the approved digest",
            ),
            TemplateSlot(
                node_id="notify_rejected",
                label="Rejection channel — receives the rejection note",
            ),
        ),
    ),
    WorkflowTemplate(
        id="vietcombank-fx",
        name="Vietcombank FX rates",
        description=(
            "Fetch Vietcombank's exchange rates with the shell tool "
            "and post the table to a channel; pairs well with a daily "
            "schedule."
        ),
        category="Digest",
        graph=_graph(
            _agent("fetch_rates", 0.0, 0.0, _FX_PROMPT),
            _notify("notify_rates", 220.0, 0.0, _FX_MESSAGE),
            edges=(
                _edge("e_fetch_notify", "fetch_rates", "notify_rates"),
            ),
        ),
        agent_slots=(
            TemplateSlot(
                node_id="fetch_rates",
                label=(
                    "Rates fetcher — pulls Vietcombank FX rates via "
                    "the shell tool and formats a table"
                ),
            ),
        ),
        channel_slots=(
            TemplateSlot(
                node_id="notify_rates",
                label="Rates channel — receives the formatted table",
            ),
        ),
    ),
)


def template_view(template: WorkflowTemplate) -> dict:
    """Project a template onto its JSON shape for the catalog listing.

    The graph is embedded whole, so the client can create from it
    directly once the install dialog has mapped the slots; the slot
    views carry only what that dialog renders.

    Args:
        template (`WorkflowTemplate`):
            The catalog entry to project.

    Returns:
        `dict`: The listing's view of the entry.
    """

    def _slot(slot: TemplateSlot) -> dict:
        return {
            "node_id": slot.node_id,
            "label": slot.label,
            "hint": slot.hint,
        }

    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "graph": template.graph.model_dump(),
        "agent_slots": [_slot(s) for s in template.agent_slots],
        "channel_slots": [_slot(s) for s in template.channel_slots],
    }


def _check_catalog() -> None:
    """Fail at import when a shipped template is not installable.

    Checks everything the strict create would trip over except the
    per-user lookups: structure (:func:`validate_workflow_graph`),
    slot coverage (every agent and notify node listed by exactly one
    slot, in node order, with the reference left empty) and every
    ``{{nodes.<id>...}}`` tag inside a prompt or message naming a
    node the graph actually holds.

    Raises:
        `ValueError`: Naming the first broken template and why.
    """
    seen: set[str] = set()
    for template in TEMPLATES:
        where = f"template {template.id!r}"
        if template.id in seen:
            raise ValueError(f"Duplicate template id {template.id!r}")
        seen.add(template.id)
        if template.category not in TEMPLATE_CATEGORIES:
            raise ValueError(
                f"{where} has unknown category {template.category!r}",
            )
        problems = validate_workflow_graph(template.graph)
        if problems:
            raise ValueError(f"{where} is invalid: {problems}")
        agent_nodes = [
            node
            for node in template.graph.nodes
            if isinstance(node.config, AgentNodeConfig)
        ]
        notify_nodes = [
            node
            for node in template.graph.nodes
            if isinstance(node.config, NotifyNodeConfig)
        ]
        for nodes, slots, ref in (
            (agent_nodes, template.agent_slots, "agent_id"),
            (notify_nodes, template.channel_slots, "channel_id"),
        ):
            if [slot.node_id for slot in slots] != [node.id for node in nodes]:
                raise ValueError(
                    f"{where}: slots {tuple(s.node_id for s in slots)} "
                    f"do not mirror nodes {tuple(n.id for n in nodes)}",
                )
            for node in nodes:
                if getattr(node.config, ref):
                    raise ValueError(
                        f"{where}: node {node.id!r} must leave {ref} "
                        "empty — it is a slot",
                    )
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
                if parts[0] == "nodes" and parts[1] not in ids:
                    raise ValueError(
                        f"{where}: node {node.id!r} references "
                        f"unknown node in {tag!r}",
                    )


_check_catalog()
