# -*- coding: utf-8 -*-
"""The discussion engines: debate driving and evaluation.

Three modules, three responsibilities —
:mod:`._debate` drives rounds (:class:`._debate.DebateEngine`),
:mod:`._evaluation` judges them and synthesizes the conclusion
(:class:`._evaluation.EvaluationEngine`), and :mod:`._turns` holds the
one-participant-one-turn plumbing both stand on. The public facade
callers depend on lives one level up: ``agentscope.app.discussion``.
"""
from ._debate import DebateEngine, RoundFailure
from ._evaluation import EvaluationEngine
from ._turns import TurnFailure

__all__ = [
    "DebateEngine",
    "EvaluationEngine",
    "RoundFailure",
    "TurnFailure",
]
