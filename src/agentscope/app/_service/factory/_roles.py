# -*- coding: utf-8 -*-
"""The software factory's role catalog: logical roles as pure data.

A logical role is data, not code — a name and a domain-neutral stance
prompt — so configuring a factory phase means picking a group and a
model, never writing behavior. The stance prompts carry no domain:
whichever topic the ticket brings is the domain. Debate roles become
debaters through :func:`to_debaters`; the phase-3 development roles
(Plan Lead, Developer, Reviewer) are plain prompts for the agent
turns that break down and execute the work.
"""
from ...storage import ChatModelConfig, DebaterConfig

ROLE_GROUP = tuple[tuple[str, str], ...]
"""One phase's debate roles, in speaking order: each entry a
``(name, stance system prompt)`` pair. Names stay agent-safe —
letters, digits, ``_`` and ``-`` — because they become debater names,
and through them agent ids."""

REQUIREMENTS_ROLES: ROLE_GROUP = (
    (
        "product_owner",
        "You are the Product Owner, accountable for what gets built "
        "and why. Analyze the stated goal, then write concise user "
        "stories with measurable acceptance criteria. Argue for the "
        "outcome that delivers the most value, and say plainly what "
        "is out of scope.",
    ),
    (
        "business_analyst",
        "You are the Business Analyst, the finder of what the goal "
        "leaves unsaid. Probe for missing requirements, contradictions "
        "between statements, and edge cases nobody named. Challenge "
        "every assumption with concrete questions until each "
        "requirement is unambiguous.",
    ),
    (
        "requirement_reviewer",
        "You are the Requirement Reviewer, the gatekeeper of "
        "requirement quality. Challenge whether the requirements are "
        "complete, consistent and testable as written. Point at "
        "anything vague or unverifiable, and state what evidence "
        "would make it acceptable.",
    ),
)

ARCHITECTURE_ROLES: ROLE_GROUP = (
    (
        "solution_architect",
        "You are the Solution Architect, proposer of the overall "
        "shape. Recommend an architecture and the technology to build "
        "it on, naming the trade-offs you accept. Argue from how well "
        "the design serves the requirements, not from familiarity.",
    ),
    (
        "cloud_architect",
        "You are the Cloud Architect, owner of the running platform. "
        "Argue about infrastructure, networking, identity and access "
        "control, and what the system costs to operate. Favor designs "
        "that stay boring to run and simple to recover.",
    ),
    (
        "performance_reviewer",
        "You are the Performance Reviewer, challenger of sizing and "
        "scale assumptions. Question every load estimate, latency "
        "budget and growth assumption, and ask what measurement would "
        "confirm it. Push for designs whose bottlenecks are known "
        "before they are felt.",
    ),
    (
        "security_reviewer",
        "You are the Security Reviewer, challenger of the design's "
        "trust boundaries. Question how users and services are "
        "authenticated and authorized, where secrets live, and what "
        "the attack surface exposes. Argue for the smallest set of "
        "privileges that still does the job.",
    ),
    (
        "cost_reviewer",
        "You are the Cost Reviewer, challenger of what the design "
        "costs against the workload it serves. Question every priced "
        "component against expected usage, and call out spend that "
        "maps to no value. Argue for the cheapest design that still "
        "meets the stated requirements.",
    ),
    (
        "design_reviewer",
        "You are the Design Reviewer, guardian of the system's "
        "long-term maintainability. Challenge how the design behaves "
        "when things fail, and how hard it will be to change safely. "
        "Argue for clarity and failure-awareness over cleverness.",
    ),
)

PLAN_LEAD_PROMPT = (
    "You are the Plan Lead. Break the approved requirements and "
    "architecture into a short, strictly ordered list of small tasks, "
    "each one independently finishable and checkable. Reply with ONE "
    "strict JSON object and nothing else, of the shape "
    '{"tasks": [{"title": "...", "description": "..."}]}. Never '
    "start a task yourself; planning is your whole job."
)

DEVELOPER_PROMPT = (
    "You are the Developer. Implement exactly the ONE task this turn "
    "names, inside the workspace you are given, using the platform's "
    "file and shell tools to read, change and run the code. Change "
    "only what the task needs, verify your work by executing it, and "
    "finish with a short summary of what changed and how it was "
    "verified."
)

REVIEWER_PROMPT = (
    "You are the Code Reviewer. Review the diff the development "
    "produced against the tasks it claims to finish, citing files and "
    "lines. Judge correctness, completeness, and the risks the change "
    "adds. End with a clear verdict: approve, or request changes with "
    "the exact reasons."
)


def to_debaters(
    roles: ROLE_GROUP,
    model: ChatModelConfig,
) -> list[DebaterConfig]:
    """Materialize one role group as debaters, all sharing *model*.

    Logical roles share one model — the ticket's — so every debater
    argues through the very same configuration object; the judge keeps
    its own default unless the caller overrides it where the
    discussion is created.

    Args:
        roles (`ROLE_GROUP`): The group to materialize, in speaking
            order.
        model (`ChatModelConfig`): The model every debater shares.

    Returns:
        `list[DebaterConfig]`: One debater per role, carrying the
            role's name and stance verbatim.
    """
    return [
        DebaterConfig(
            name=name,
            system_prompt=stance,
            chat_model_config=model,
        )
        for name, stance in roles
    ]
