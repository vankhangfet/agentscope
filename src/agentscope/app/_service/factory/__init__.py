# -*- coding: utf-8 -*-
"""The software factory's service layer.

:mod:`._roles` holds the role catalog — pure data: the debate groups
per phase and the phase-3 development prompts. :mod:`._engine` walks
a ticket through its phases over them (:class:`._engine.FactoryEngine`
— discussions, develop turns, gates, modes and recovery).
"""
from ._engine import FactoryEngine, PhaseFailure
from ._roles import (
    ARCHITECTURE_ROLES,
    DEVELOPER_PROMPT,
    PLAN_LEAD_PROMPT,
    REQUIREMENTS_ROLES,
    REVIEWER_PROMPT,
    ROLE_GROUP,
    to_debaters,
)

__all__ = [
    "ARCHITECTURE_ROLES",
    "DEVELOPER_PROMPT",
    "FactoryEngine",
    "PLAN_LEAD_PROMPT",
    "PhaseFailure",
    "REQUIREMENTS_ROLES",
    "REVIEWER_PROMPT",
    "ROLE_GROUP",
    "to_debaters",
]
