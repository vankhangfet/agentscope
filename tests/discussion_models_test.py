# -*- coding: utf-8 -*-
"""Discussion record models: the persisted state a debate walks
through. Pure pydantic, no I/O."""
from datetime import datetime
from unittest import TestCase

from pydantic import ValidationError

from agentscope.app.storage import (
    ChatModelConfig,
    DebaterConfig,
    DiscussionData,
    DiscussionOrigin,
    DiscussionRecord,
    DiscussionStatus,
    JudgeConfig,
    RoundArgument,
    RoundEvaluation,
    SessionConfig,
    SessionRecord,
)
from agentscope.state import AgentState


def _chat_model() -> ChatModelConfig:
    """A minimal but complete chat model config."""
    return ChatModelConfig(
        type="openai",
        credential_id="cred-1",
        model="gpt-4o",
        parameters={},
    )


def _debater(name: str) -> DebaterConfig:
    """A debater arguing one side of the fixture topic."""
    return DebaterConfig(
        name=name,
        system_prompt=f"You argue for {name}.",
        chat_model_config=_chat_model(),
    )


def _discussion_data(**overrides: object) -> DiscussionData:
    """A two-debater discussion, minus whatever a test overrides."""
    payload: dict = dict(
        topic="cats vs dogs",
        debaters=[_debater("pro"), _debater("con")],
        judge=JudgeConfig(chat_model_config=_chat_model()),
    )
    payload.update(overrides)
    return DiscussionData(**payload)


class DiscussionModelsTest(TestCase):
    """Shapes, defaults and JSON round-trips."""

    def test_status_values(self) -> None:
        self.assertEqual(
            {s.value for s in DiscussionStatus},
            {
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            },
        )

    def test_defaults(self) -> None:
        data = _discussion_data()
        self.assertEqual(data.status, DiscussionStatus.PENDING)
        self.assertEqual(data.max_rounds, 3)
        self.assertEqual(data.rounds, [])
        self.assertIsNone(data.conclusion)
        self.assertIsNone(data.error)
        self.assertIsNone(data.current_speaker)
        self.assertEqual(data.trigger, "manual")
        self.assertIsNone(data.finished_at)
        # The judge stays neutral out of the box.
        self.assertTrue(
            data.judge.system_prompt.startswith(
                "You are an impartial debate judge",
            ),
        )
        # An argument awaits its evaluation.
        argument = RoundArgument(debater="pro", text="cats purr")
        self.assertIsNone(argument.strength)
        self.assertIsNone(argument.rebutted)
        self.assertEqual(argument.note, "")
        evaluation = RoundEvaluation(
            round=1,
            arguments=[argument],
        )
        self.assertFalse(evaluation.consensus)
        self.assertEqual(evaluation.summary, "")

    def test_max_rounds_bounds(self) -> None:
        for bad in (0, 11):
            with self.assertRaises(ValidationError):
                _discussion_data(max_rounds=bad)
        # The boundaries themselves are legal.
        self.assertEqual(_discussion_data(max_rounds=1).max_rounds, 1)
        self.assertEqual(_discussion_data(max_rounds=10).max_rounds, 10)

    def test_debaters_min_one(self) -> None:
        with self.assertRaises(ValidationError):
            _discussion_data(debaters=[])

    def test_assignment_is_validated(self) -> None:
        data = _discussion_data()
        with self.assertRaises(ValidationError):
            data.status = "BOGUS"
        with self.assertRaises(ValidationError):
            data.max_rounds = 99
        data.status = "COMPLETED"
        self.assertEqual(data.status, DiscussionStatus.COMPLETED)
        # The judge's fill-in of an argument revalidates too.
        argument = RoundArgument(debater="pro", text="cats purr")
        with self.assertRaises(ValidationError):
            argument.strength = 9
        argument.strength = 4
        self.assertEqual(argument.strength, 4)

    def test_record_round_trips_through_json(self) -> None:
        record = DiscussionRecord(
            user_id="u1",
            data=_discussion_data(
                status=DiscussionStatus.COMPLETED,
                conclusion="cats, narrowly",
                rounds=[
                    RoundEvaluation(
                        round=1,
                        arguments=[
                            RoundArgument(
                                debater="pro",
                                text="cats purr",
                                strength=4,
                                rebutted=False,
                                note="solid",
                            ),
                            RoundArgument(
                                debater="con",
                                text="dogs fetch",
                            ),
                        ],
                        consensus=False,
                        summary="close round",
                    ),
                ],
                finished_at=datetime(2026, 10, 16, 12, 0),
            ),
        )
        restored = DiscussionRecord.model_validate_json(
            record.model_dump_json(),
        )
        self.assertEqual(restored, record)
        self.assertEqual(
            restored.data.rounds[0].arguments[0].note,
            "solid",
        )
        self.assertEqual(
            restored.data.finished_at,
            datetime(2026, 10, 16, 12, 0),
        )

    def test_current_speaker_round_trips(self) -> None:
        """The live floor-holder survives JSON, debater or judge."""
        data = _discussion_data()
        for speaker in ("pro", "judge"):
            data.current_speaker = speaker
            restored = DiscussionData.model_validate_json(
                data.model_dump_json(),
            )
            self.assertEqual(restored.current_speaker, speaker)

    def test_discussion_origin_round_trips_in_a_session(self) -> None:
        """A discussion-opened session carries its origin through JSON."""
        record = SessionRecord(
            user_id="u1",
            agent_id="ag1",
            config=SessionConfig(workspace_id="ws", name="n"),
            state=AgentState(),
            origin=DiscussionOrigin(
                discussion_id="disc-1",
                debater="pro",
            ),
        )
        restored = SessionRecord.model_validate_json(
            record.model_dump_json(),
        )
        self.assertEqual(restored.origin.type, "discussion")
        self.assertEqual(restored.origin.discussion_id, "disc-1")
        self.assertEqual(restored.origin.debater, "pro")
