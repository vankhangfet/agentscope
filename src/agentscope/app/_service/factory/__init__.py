# -*- coding: utf-8 -*-
"""The software factory's service layer.

:mod:`._roles` holds the role catalog — pure data: the debate groups
per phase and the phase-3 development prompts. The engines over them
(the ticket walker with its gates and recovery) join this package as
their tasks land.
"""
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
    "PLAN_LEAD_PROMPT",
    "REQUIREMENTS_ROLES",
    "REVIEWER_PROMPT",
    "ROLE_GROUP",
    "to_debaters",
]
