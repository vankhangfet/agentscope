# -*- coding: utf-8 -*-
"""Factory ticket record models: the persisted state a ticket walks
through. Pure pydantic, no I/O."""
from datetime import datetime
from unittest import TestCase

from pydantic import ValidationError

from agentscope.app.storage import (
    ChatModelConfig,
    FactoryDecision,
    FactoryMode,
    FactoryPhase,
    FactoryStatus,
    FactoryTask,
    FactoryTicketData,
    FactoryTicketRecord,
)


def _chat_model() -> ChatModelConfig:
    """A minimal but complete chat model config."""
    return ChatModelConfig(
        type="openai",
        credential_id="cred-1",
        model="gpt-4o",
        parameters={},
    )


def _ticket_data(**overrides: object) -> FactoryTicketData:
    """A fresh ticket, minus whatever a test overrides."""
    payload: dict = dict(
        title="ship the login page",
        description="Users cannot sign in.",
        chat_model_config=_chat_model(),
    )
    payload.update(overrides)
    return FactoryTicketData(**payload)


class FactoryModelsTest(TestCase):
    """Shapes, defaults and JSON round-trips."""

    def test_mode_values(self) -> None:
        self.assertEqual(
            {m.value for m in FactoryMode},
            {"COLLABORATIVE", "DELEGATED"},
        )

    def test_phase_values(self) -> None:
        self.assertEqual(
            {p.value for p in FactoryPhase},
            {"REQUIREMENTS", "ARCHITECTURE", "DEVELOP"},
        )

    def test_status_values(self) -> None:
        self.assertEqual(
            {s.value for s in FactoryStatus},
            {
                "PENDING",
                "RUNNING",
                "WAITING_HUMAN",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            },
        )

    def test_defaults(self) -> None:
        data = _ticket_data()
        self.assertEqual(data.mode, FactoryMode.COLLABORATIVE)
        self.assertEqual(data.phase, FactoryPhase.REQUIREMENTS)
        self.assertEqual(data.status, FactoryStatus.PENDING)
        self.assertEqual(data.discussions, {})
        self.assertIsNone(data.requirements)
        self.assertIsNone(data.architecture)
        self.assertEqual(data.tasks, [])
        self.assertIsNone(data.review)
        self.assertEqual(data.decisions, [])
        self.assertIsNone(data.workspace_id)
        self.assertIsNone(data.error)
        self.assertEqual(data.trigger, "manual")
        self.assertIsNone(data.finished_at)
        # A task starts untouched, whoever it is assigned to.
        task = FactoryTask(title="write the form")
        self.assertEqual(task.description, "")
        self.assertEqual(task.assignee, "")
        self.assertEqual(task.status, "PENDING")
        self.assertIsNone(task.output)

    def test_bad_enum_values_are_rejected(self) -> None:
        for bad in (
            {"mode": "SOLO"},
            {"phase": "DESIGN"},
            {"status": "PAUSED"},
        ):
            with self.assertRaises(ValidationError):
                _ticket_data(**bad)
        with self.assertRaises(ValidationError):
            FactoryDecision(
                phase="DESIGN",
                discussion_id=None,
                conclusion="c",
                consensus=True,
                created_at=datetime(2026, 10, 18, 12, 0),
            )

    def test_bad_task_status_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            FactoryTask(title="write the form", status="COMPLETED")
        # Every legal task status validates.
        for status in ("PENDING", "RUNNING", "DONE", "FAILED"):
            self.assertEqual(
                FactoryTask(title="t", status=status).status,
                status,
            )

    def test_assignment_is_validated(self) -> None:
        data = _ticket_data()
        with self.assertRaises(ValidationError):
            data.status = "BOGUS"
        with self.assertRaises(ValidationError):
            data.phase = "DESIGN"
        data.status = FactoryStatus.WAITING_HUMAN
        self.assertEqual(data.status, FactoryStatus.WAITING_HUMAN)
        # The engine's in-place task updates revalidate too.
        task = FactoryTask(title="write the form")
        with self.assertRaises(ValidationError):
            task.status = "COMPLETED"
        task.status = "RUNNING"
        task.output = "done"
        self.assertEqual(task.status, "RUNNING")
        self.assertEqual(task.output, "done")

    def test_record_round_trips_through_json(self) -> None:
        record = FactoryTicketRecord(
            user_id="u1",
            data=_ticket_data(
                mode=FactoryMode.DELEGATED,
                phase=FactoryPhase.DEVELOP,
                status=FactoryStatus.WAITING_HUMAN,
                discussions={
                    FactoryPhase.REQUIREMENTS: "disc-1",
                    FactoryPhase.ARCHITECTURE: "disc-2",
                },
                requirements="must sign in",
                architecture="three tiers",
                tasks=[
                    FactoryTask(
                        title="write the form",
                        description="an html form",
                        assignee="Developer",
                        status="DONE",
                        output="form written",
                    ),
                    FactoryTask(
                        title="wire the api",
                        assignee="Developer",
                        status="RUNNING",
                    ),
                ],
                review=None,
                decisions=[
                    FactoryDecision(
                        phase=FactoryPhase.REQUIREMENTS,
                        discussion_id="disc-1",
                        conclusion="login is needed",
                        note="approved",
                        consensus=True,
                        created_at=datetime(2026, 10, 17, 9, 0),
                    ),
                    FactoryDecision(
                        phase=FactoryPhase.ARCHITECTURE,
                        discussion_id="disc-2",
                        conclusion="three tiers, no consensus",
                        note="human picked anyway",
                        consensus=False,
                        created_at=datetime(2026, 10, 18, 12, 0),
                    ),
                ],
                workspace_id="ws-1",
                finished_at=None,
            ),
        )
        restored = FactoryTicketRecord.model_validate_json(
            record.model_dump_json(),
        )
        self.assertEqual(restored, record)
        # The phase-keyed discussions dict comes back keyed by phase.
        self.assertEqual(
            restored.data.discussions,
            {
                FactoryPhase.REQUIREMENTS: "disc-1",
                FactoryPhase.ARCHITECTURE: "disc-2",
            },
        )
        self.assertEqual(restored.data.tasks[0].status, "DONE")
        self.assertEqual(
            restored.data.decisions[1].note,
            "human picked anyway",
        )
        self.assertEqual(restored.data.workspace_id, "ws-1")
