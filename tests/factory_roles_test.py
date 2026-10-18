# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""The factory role catalog and the debate run-to-completion wrapper.

The catalog is pure data, checked here by shape only; the wrapper is
driven on fakeredis exactly like the discussion engine tests — the
engine constructed by hand, the deterministic core awaited inline
(no dispatch) — reusing that file's fake runner and helpers.
"""
import re
from unittest import IsolatedAsyncioTestCase, TestCase

from fastapi import HTTPException

from agentscope.app._service.discussion import DebateEngine
from agentscope.app._service.factory import (
    ARCHITECTURE_ROLES,
    DEVELOPER_PROMPT,
    PLAN_LEAD_PROMPT,
    REQUIREMENTS_ROLES,
    REVIEWER_PROMPT,
    to_debaters,
)
from agentscope.app.storage import DebaterConfig, DiscussionStatus
from discussion_engine_test import (
    USER,
    _FakeRunner,
    _record,
    make_storage,
    _model,
)

_GROUPS = (REQUIREMENTS_ROLES, ARCHITECTURE_ROLES)
"""Every debate group the catalog ships."""

_DOMAIN_WORDS = ("iot", "aws", "kubernetes")
"""Spot-check words no domain-neutral stance prompt may contain."""


class RoleCatalogTest(TestCase):
    """The catalog's shape: pure, agent-safe, domain-neutral data."""

    def test_every_group_holds_at_least_three_roles(self) -> None:
        for group in _GROUPS:
            self.assertGreaterEqual(
                len(group),
                3,
                msg=f"group of {len(group)} roles is too thin",
            )

    def test_names_unique_within_and_across_groups(self) -> None:
        names = [name for group in _GROUPS for name, _stance in group]
        self.assertEqual(
            len(names),
            len(set(names)),
            msg=f"role names repeat: {sorted(names)}",
        )

    def test_names_are_agent_safe(self) -> None:
        """Names become debater names (and agents): no spaces or other
        characters an agent id could not carry."""
        for group in _GROUPS:
            for name, _stance in group:
                self.assertRegex(name, r"^[a-zA-Z0-9_-]+$")

    def test_stances_are_domain_neutral(self) -> None:
        for group in _GROUPS:
            for name, stance in group:
                for word in _DOMAIN_WORDS:
                    self.assertNotIn(
                        word,
                        stance.lower(),
                        msg=f"{name!r} stance names the domain: {word!r}",
                    )

    def test_to_debaters_shares_one_model_and_keeps_stances(self) -> None:
        model = _model()
        for group in _GROUPS:
            debaters = to_debaters(group, model)
            self.assertEqual(len(debaters), len(group))
            for debater, (name, stance) in zip(debaters, group):
                self.assertIsInstance(debater, DebaterConfig)
                self.assertEqual(debater.name, name)
                self.assertEqual(debater.system_prompt, stance)
                self.assertTrue(debater.system_prompt.strip())
                # Logical roles share one model: the ticket's.
                self.assertIs(debater.chat_model_config, model)

    def test_development_prompts_present(self) -> None:
        for prompt in (PLAN_LEAD_PROMPT, DEVELOPER_PROMPT, REVIEWER_PROMPT):
            self.assertTrue(prompt.strip())
        # The Plan Lead's whole contract is a strict-JSON task list.
        self.assertIn("JSON", PLAN_LEAD_PROMPT)


class RunToCompletionTest(IsolatedAsyncioTestCase):
    """The public wrapper: finish a discussion inline."""

    async def test_runs_rounds_without_dispatch(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)
        saved = await storage.upsert_discussion(USER, _record(max_rounds=1))
        engine = DebateEngine(storage, runner)

        done = await engine.run_to_completion(USER, saved.id)

        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)
        self.assertTrue(done.data.conclusion)
        self.assertEqual(len(done.data.rounds), 1)
        # Ran inline: no loop task was ever registered.
        self.assertNotIn(saved.id, engine._tasks)

    async def test_awaits_a_live_loop_task(self) -> None:
        storage = make_storage()
        runner = _FakeRunner(storage)
        saved = await storage.upsert_discussion(USER, _record(max_rounds=1))
        engine = DebateEngine(storage, runner)
        await engine.dispatch(USER, saved.id)

        done = await engine.run_to_completion(USER, saved.id)

        self.assertIs(done.data.status, DiscussionStatus.COMPLETED)
        self.assertTrue(done.data.conclusion)

    async def test_unknown_discussion_404s(self) -> None:
        storage = make_storage()
        engine = DebateEngine(storage, _FakeRunner(storage))
        with self.assertRaises(HTTPException) as caught:
            await engine.run_to_completion(USER, "nope")
        self.assertEqual(caught.exception.status_code, 404)
