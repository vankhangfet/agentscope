# -*- coding: utf-8 -*-
"""The software factory's role catalog: logical roles as pure data.

A logical role is data, not code — a name and a domain-neutral stance
prompt — so configuring a factory phase means picking a group and a
model, never writing behavior. The stance prompts carry no domain:
whichever topic the ticket brings is the domain. Debate roles become
debaters through :func:`to_debaters`; the phase-3 development roles
(Plan Lead, Developer, Reviewer) are plain prompts for the agent
turns that break down and execute the work.

The roster projections (:data:`ROLE_TITLES`, :data:`DEVELOPMENT_ROLES`
and :func:`factory_roster`) surface the very same catalog for display
— titles and per-role stances for humans — without touching what
runs: every debate still goes through the groups and prompts above.
"""
from ...storage import (
    ChatModelConfig,
    DebaterConfig,
    FactoryPhase,
)

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

ROLE_TITLES: dict[str, str] = {
    "product_owner": "Product Owner",
    "business_analyst": "Business Analyst",
    "requirement_reviewer": "Requirement Reviewer",
    "solution_architect": "Solution Architect",
    "cloud_architect": "Cloud Architect",
    "performance_reviewer": "Performance Reviewer",
    "security_reviewer": "Security Reviewer",
    "cost_reviewer": "Cost Reviewer",
    "design_reviewer": "Design Reviewer",
    "plan_lead": "Plan Lead",
    "developer": "Developer",
    "reviewer": "Reviewer",
}
"""Every role name → the display title the roster shows."""

DEVELOPMENT_ROLES: ROLE_GROUP = (
    (
        "plan_lead",
        "Breaks the approved requirements and architecture into a "
        "short, strictly ordered list of small, independently "
        "checkable tasks. Planning is the whole job — the Plan Lead "
        "never starts a task itself.",
    ),
    (
        "developer",
        "Implements exactly one task at a time in the ticket's "
        "workspace, changing only what the task needs and verifying "
        "the work by running it.",
    ),
    (
        "reviewer",
        "Reviews the development against the tasks it claims to "
        "finish — correctness, completeness, risk — and ends with a "
        "clear verdict: approve, or request changes.",
    ),
)
"""The DEVELOP phase's roles for display. Their turn prompts above
(:data:`PLAN_LEAD_PROMPT`, :data:`DEVELOPER_PROMPT`,
:data:`REVIEWER_PROMPT`) instruct a single agent turn each; these
stances instead describe the role's job, which is what a roster
reader wants. The ``developer`` covers the per-task
``developer_N`` sessions."""

JUDGE_ROLE: dict[str, str] = {
    "name": "judge",
    "title": "Judge",
    "stance": (
        "Scores every round's arguments and watches for genuine "
        "agreement between the roles. A debate concludes when "
        "consensus is detected — never forced."
    ),
}
"""The judge both debate phases share, as the roster shows it. The
name mirrors the discussion engine's judge key."""

_GROUP_SPECS: tuple[tuple[FactoryPhase, str, str, ROLE_GROUP], ...] = (
    (
        FactoryPhase.REQUIREMENTS,
        "Requirements debate",
        "Argues the problem into testable requirements.",
        REQUIREMENTS_ROLES,
    ),
    (
        FactoryPhase.ARCHITECTURE,
        "Architecture debate",
        "Argues the system's shape, stack and trade-offs.",
        ARCHITECTURE_ROLES,
    ),
    (
        FactoryPhase.DEVELOP,
        "Development",
        "Breaks the approved design into tasks, then builds and "
        "reviews each one.",
        DEVELOPMENT_ROLES,
    ),
)
"""The roster's groups in phase order: phase, title, one-line
description and the role group the phase runs."""


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


def factory_roster() -> list[dict]:
    """Project the whole catalog as the roster the API serves.

    Pure data in phase order: one dict per group — its phase, title,
    description and its roles as ``{name, title, stance}`` view dicts,
    each stance taken verbatim from the group the engine runs and each
    title from :data:`ROLE_TITLES`. The judge is served separately as
    :data:`JUDGE_ROLE` because both debate phases share it.

    Returns:
        `list[dict]`: The role groups, in phase order.
    """
    return [
        {
            "phase": phase,
            "title": title,
            "description": description,
            "roles": [
                {
                    "name": name,
                    "title": ROLE_TITLES[name],
                    "stance": stance,
                }
                for name, stance in roles
            ],
        }
        for phase, title, description, roles in _GROUP_SPECS
    ]
