# -*- coding: utf-8 -*-
"""The evaluation half of a discussion: judging rounds and synthesizing
the conclusion.

The EvaluationEngine owns the judge's session and nothing else. After
each round it asks the judge to score every argument (a 1-5 strength, a
rebutted flag, a one-line note) and to say whether the sides have
converged; at stop it asks the same judge for the final conclusion over
all rounds. A judge that cannot or will not produce a readable verdict
degrades the round to unevaluated rather than failing the debate — the
arguments are the record's substance, the scores are enrichment.
"""
import asyncio
import json

from ...storage import (
    DiscussionRecord,
    RoundArgument,
    RoundEvaluation,
    StorageBase,
)
from ._turns import (
    ChatRunner,
    JUDGE_KEY,
    ParticipantSessions,
    read_turn_reply,
    run_turn,
)

_VERDICT_SHAPE = (
    "Assess every argument strictly on its merits and whether the "
    "sides have converged. Reply with ONE JSON object and nothing "
    "else — no prose, no code fences — in exactly this shape:\n"
    '{"assessments": [{"debater": "<name>", "strength": <1-5>, '
    '"rebutted": <true|false>, "note": "<one line>"}], '
    '"consensus": <true|false>, "summary": "<one paragraph>"}'
)

_RETRY_NUDGE = (
    "\n\nYour previous reply was not the required JSON. Reply again "
    "with the JSON object only."
)


class EvaluationEngine:
    """Judges rounds, detects consensus, synthesizes conclusions."""

    def __init__(
        self,
        storage: StorageBase,
        chat_runner: ChatRunner,
    ) -> None:
        """Initialize the engine.

        Args:
            storage (`StorageBase`): Application storage.
            chat_runner (`ChatRunner`): Drives one agent turn to
                completion (``ChatService.run`` in production).
        """
        self._storage = storage
        self._chat_runner = chat_runner
        self._sessions = ParticipantSessions(storage)

    async def evaluate_round(
        self,
        user_id: str,
        record: DiscussionRecord,
        round_idx: int,
        arguments: list[RoundArgument],
    ) -> RoundEvaluation:
        """Ask the judge to verdict one round's *arguments*.

        The judge sees the topic and the round's arguments in speaking
        order, and must reply with the strict JSON verdict. A reply
        that does not parse gets one retry with a stricter nudge; a
        second failure — or a judge turn that itself fails — degrades
        to an unevaluated round (every strength stays ``None``,
        consensus stays ``False``, the summary says why) so the debate
        itself survives its judge.

        Args:
            user_id (`str`): The discussion's owner.
            record (`DiscussionRecord`): The discussion being argued.
            round_idx (`int`): Which round, 1-based.
            arguments (`list[RoundArgument]`): The round's arguments,
                in speaking order.

        Returns:
            `RoundEvaluation`: The verdict applied onto copies of the
                arguments — unevaluated copies when the judge failed.
        """
        judge = record.data.judge
        session_id, agent_id = await self._sessions.ensure(
            user_id,
            record,
            JUDGE_KEY,
            judge.system_prompt,
            judge.chat_model_config,
        )
        prompt = _verdict_prompt(record, round_idx, arguments)
        failure: str | None = None
        for attempt in (1, 2):
            try:
                await run_turn(
                    self._chat_runner,
                    user_id,
                    session_id,
                    agent_id,
                    prompt + (_RETRY_NUDGE if attempt > 1 else ""),
                )
                reply = await read_turn_reply(
                    self._storage,
                    user_id,
                    session_id,
                    "judge",
                )
                parsed = _parse_verdict(reply)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-except
                # A judge turn that itself failed — prod closes failed
                # turns as error-bearing replies, but a runner that
                # raises gets the same treatment: retry, then degrade.
                failure = str(exc)
                continue
            if parsed is not None:
                return _apply_verdict(round_idx, arguments, parsed)
            failure = "judge reply was not the required JSON"
        return RoundEvaluation(
            round=round_idx,
            arguments=[argument.model_copy() for argument in arguments],
            consensus=False,
            summary=f"judge unavailable ({failure})",
        )

    async def synthesize(
        self,
        user_id: str,
        record: DiscussionRecord,
    ) -> str:
        """Ask the judge for the final conclusion over every round.

        Unlike a round verdict this is the discussion's deliverable, so
        a failure propagates to the caller (the debate engine fails the
        discussion) rather than degrading.

        Args:
            user_id (`str`): The discussion's owner.
            record (`DiscussionRecord`): The finished discussion, its
                rounds all persisted.

        Returns:
            `str`: The conclusion text.
        """
        judge = record.data.judge
        session_id, agent_id = await self._sessions.ensure(
            user_id,
            record,
            JUDGE_KEY,
            judge.system_prompt,
            judge.chat_model_config,
        )
        prompt = _conclusion_prompt(record)
        await run_turn(
            self._chat_runner,
            user_id,
            session_id,
            agent_id,
            prompt,
        )
        return await read_turn_reply(
            self._storage,
            user_id,
            session_id,
            "judge",
        )


def _verdict_prompt(
    record: DiscussionRecord,
    round_idx: int,
    arguments: list[RoundArgument],
) -> str:
    """Render the judge's round-verdict prompt: topic plus arguments."""
    lines = [f"Topic: {record.data.topic}", ""]
    lines.append(
        f"Round {round_idx} of {record.data.max_rounds} — the "
        "arguments, in speaking order:",
    )
    lines.append("")
    for argument in arguments:
        lines.append(f"[{argument.debater}] {argument.text}")
    lines.append("")
    lines.append(_VERDICT_SHAPE)
    return "\n".join(lines)


def _conclusion_prompt(record: DiscussionRecord) -> str:
    """Render the judge's conclusion prompt: every round, then the ask."""
    lines = [f"Topic: {record.data.topic}", ""]
    for evaluation in record.data.rounds:
        lines.append(f"Round {evaluation.round}:")
        if evaluation.summary:
            lines.append(f"Summary: {evaluation.summary}")
        for argument in evaluation.arguments:
            lines.append(f"[{argument.debater}] {argument.text}")
        lines.append("")
    lines.append(
        "The debate is over. Write the final conclusion: where the "
        "debaters ended up, the strongest points each side made, and "
        "the overall outcome. Stay within what the arguments above "
        "actually support.",
    )
    return "\n".join(lines)


def _parse_verdict(text: str) -> dict | None:
    """Parse the judge's reply into the verdict object, or None.

    Extraction is brace-scanning rather than a bare ``json.loads`` on
    the whole reply — the same tolerant-reading discipline the
    workflow judge applies to labels: a model that wraps its JSON in
    prose or a code fence is still read, while anything that is not
    exactly one JSON object of the required shape (``assessments`` a
    list, ``consensus`` a bool) is a parse failure that triggers the
    retry.
    """
    candidate = _extract_json_object(text)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("assessments"), list):
        return None
    if not isinstance(parsed.get("consensus"), bool):
        return None
    return parsed


def _extract_json_object(text: str) -> str | None:
    """The first balanced ``{...}`` substring of *text*, or None.

    The scan tracks nesting and string escaping, so braces inside
    strings or nested objects do not end the object early, and prose
    or code fences around the JSON are ignored.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _apply_verdict(
    round_idx: int,
    arguments: list[RoundArgument],
    parsed: dict,
) -> RoundEvaluation:
    """Copy the arguments and fold the parsed assessments onto them.

    Assessments are matched to arguments by debater name; anything the
    judge got wrong on an entry (an unknown name, a strength outside
    1-5 or not a number, a non-boolean ``rebutted``) leaves that
    argument's field unevaluated rather than failing the verdict.
    """
    evaluation = RoundEvaluation(
        round=round_idx,
        arguments=[
            argument.model_copy() for argument in arguments
        ],
        consensus=parsed["consensus"],
        summary=(
            str(parsed["summary"])
            if isinstance(parsed.get("summary"), str)
            else ""
        ),
    )
    by_name = {
        argument.debater: argument for argument in evaluation.arguments
    }
    for assessment in parsed["assessments"]:
        if not isinstance(assessment, dict):
            continue
        target = by_name.get(str(assessment.get("debater", "")))
        if target is None:
            continue
        strength = assessment.get("strength")
        if (
            isinstance(strength, int)
            and not isinstance(strength, bool)
            and 1 <= strength <= 5
        ):
            target.strength = strength
        rebutted = assessment.get("rebutted")
        if isinstance(rebutted, bool):
            target.rebutted = rebutted
        note = assessment.get("note")
        if isinstance(note, str):
            target.note = note
    return evaluation
