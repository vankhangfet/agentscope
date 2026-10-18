# -*- coding: utf-8 -*-
"""The debate half of a discussion: driving rounds to a conclusion.

The DebateEngine owns every persisted transition of a discussion, the
way the workflow engine owns a run's: each transition is persisted
before the step that depends on it, so a crash always leaves a record
the boot sweep (:meth:`DebateEngine.recover_stale`) can classify — a
discussion still PENDING or RUNNING was interrupted mid-flight and is
failed out as ``interrupted by restart``. Round flow, mirroring the
design: debaters argue sequentially (each seeing the prior rounds'
arguments through its own accumulating session plus the rendered
latest arguments of the others), the EvaluationEngine judges the
round, and the debate stops on max_rounds, judge consensus, cancel or
failure — then the judge synthesizes the conclusion. Rounds persist
INCREMENTALLY — an empty round lands before its first turn, each
argument as it is delivered, ``current_speaker`` announcing who holds
the floor — so a reader (the UI) sees a debate live instead of one
whole round at a time.
"""
import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from fastapi import HTTPException, status

from ...storage import (
    DebaterConfig,
    DiscussionRecord,
    DiscussionStatus,
    RoundArgument,
    RoundEvaluation,
    StorageBase,
)
from ._evaluation import EvaluationEngine
from ._turns import (
    ChatRunner,
    JUDGE_KEY,
    ParticipantSessions,
    read_turn_reply,
    run_turn,
)

logger = logging.getLogger(__name__)

_TERMINAL = {
    DiscussionStatus.COMPLETED,
    DiscussionStatus.FAILED,
    DiscussionStatus.CANCELLED,
}

_RESTART_ERROR = "interrupted by restart"


class RoundFailure(Exception):
    """A round could not be argued; message is user-facing and names
    the round it died in."""


class DebateEngine:
    """Drives discussions: rounds, stop conditions, the conclusion.

    One task per discussion per process — :meth:`dispatch` is
    idempotent through an atomic check-and-register with no await in
    between (the workflow engine's discipline). Every read-modify-write
    of a discussion record goes through :meth:`_mutate`, which
    serialises transitions on one write lock and reloads inside it.
    """

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
        self._evaluation = EvaluationEngine(storage, chat_runner)
        self._sessions = ParticipantSessions(storage)
        self._tasks: dict[str, asyncio.Task] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Dispatch and lifecycle
    # ------------------------------------------------------------------

    async def dispatch(self, user_id: str, discussion_id: str) -> None:
        """Start the debate loop for a discussion, idempotently.

        The live-task check and the registration below have no await
        between them, so they are atomic under asyncio's cooperative
        scheduling: of two racing dispatch calls exactly one spawns a
        loop. The body never yields — the PENDING→RUNNING flip and the
        terminal pre-checks live in the loop head, where a crash leaves
        a record the sweep can classify.

        Args:
            user_id (`str`): The discussion's owner.
            discussion_id (`str`): The discussion to drive.
        """
        if self._closed:
            # A closed engine drives nothing; the next process's
            # recovery sweep picks the discussion up.
            return
        live = self._tasks.get(discussion_id)
        if live is not None and not live.done():
            return
        # create_task schedules without running the loop, so the task
        # is registered — and visible to any concurrent dispatch —
        # before this coroutine can yield.
        task = asyncio.create_task(self._loop(user_id, discussion_id))
        self._tasks[discussion_id] = task
        task.add_done_callback(
            lambda done, did=discussion_id: self._forget_task(did, done),
        )

    def _forget_task(self, discussion_id: str, task: asyncio.Task) -> None:
        """Drop the discussion's loop task once it ends, logging any
        crash that escaped the loop's own handler."""
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Discussion %r loop crashed.",
                discussion_id,
                exc_info=task.exception(),
            )
        if self._tasks.get(discussion_id) is task:
            self._tasks.pop(discussion_id, None)

    async def _loop(self, user_id: str, discussion_id: str) -> None:
        """Run :meth:`_run_to_completion` for one discussion, failing
        the discussion on anything that escapes it.

        The completion body already translates argument and round
        failures into FAILED records, so what reaches this handler is
        an engine-level fault (storage between transitions, a bug) —
        recorded as the discussion's error when it can be, logged
        always. Cancellation passes through untouched: :meth:`cancel`
        persists CANCELLED once the task settles, and a shutdown cancel
        (:meth:`close`) deliberately leaves the record RUNNING for the
        next process's sweep.
        """
        try:
            await self._run_to_completion(user_id, discussion_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Discussion %r loop crashed.",
                discussion_id,
                exc_info=exc,
            )
            try:
                await self._fail(user_id, discussion_id, str(exc))
            except Exception:  # pylint: disable=broad-except
                logger.error(
                    "Failing discussion %r after a loop crash failed.",
                    discussion_id,
                    exc_info=True,
                )

    async def run_to_completion(
        self,
        user_id: str,
        discussion_id: str,
    ) -> DiscussionRecord:
        """Drive one discussion to its settled record and return it.

        For orchestrators and tests that need a discussion finished
        inline rather than dispatched — the factory's phases await
        their discussions through here. When this engine is already
        driving the discussion, that live loop task is awaited (never
        raced with a second completion body); otherwise the
        deterministic core runs directly in this coroutine.

        Must NOT be called from the discussion's own loop task (that
        would await itself); callers are sibling tasks such as the
        factory's phase loop.

        Args:
            user_id (`str`): The discussion's owner.
            discussion_id (`str`): The discussion to finish.

        Returns:
            `DiscussionRecord`: The record as it settled — terminal
                after a normal run or a ``cancel`` (which persists
                CANCELLED before returning); still RUNNING only when
                ``close`` tore the loop down mid-run, in which case
                the next boot's sweep classifies it.

        Raises:
            `HTTPException`: 404 when the discussion is missing.
        """
        record = await self._storage.get_discussion(user_id, discussion_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Discussion {discussion_id!r} not found.",
            )
        live = self._tasks.get(discussion_id)
        if live is not None and not live.done():
            try:
                await live
            except asyncio.CancelledError:
                if not live.cancelled():
                    # The cancellation was aimed at THIS task (the
                    # caller being cancelled or shut down), not at the
                    # debate — let it propagate instead of swallowing
                    # the caller's outcome.
                    raise
                # The debate's own loop was cancelled (an internal
                # cancel persists CANCELLED; close() leaves RUNNING
                # for the boot sweep) — the record reloaded below
                # carries whichever outcome landed.
        else:
            await self._run_to_completion(user_id, discussion_id)
        final = await self._storage.get_discussion(user_id, discussion_id)
        if final is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Discussion {discussion_id!r} not found.",
            )
        return final

    async def _run_to_completion(
        self,
        user_id: str,
        discussion_id: str,
    ) -> None:
        """Drive one discussion from PENDING to a terminal record.

        The deterministic core the loop task wraps — tests await this
        directly instead of racing a background task. Every round is
        preceded by a fresh reload (so a cancel between rounds is seen)
        and persisted incrementally inside :meth:`_run_round`, the
        verdict landing before the stop decision reads it. The
        conclusion turn runs only on a debate that actually stopped
        non-terminal; its failure fails the discussion — unlike a round
        verdict, the conclusion is the deliverable.

        Args:
            user_id (`str`): The discussion's owner.
            discussion_id (`str`): The discussion to drive.
        """
        record = await self._storage.get_discussion(user_id, discussion_id)
        if record is None:
            logger.warning(
                "Dispatch skipped: discussion %r not found.",
                discussion_id,
            )
            return
        if record.data.status in _TERMINAL:
            return
        if record.data.status is DiscussionStatus.PENDING:
            await self._mutate(user_id, discussion_id, _mark_running)

        while True:
            record = await self._storage.get_discussion(user_id, discussion_id)
            if record is None or record.data.status in _TERMINAL:
                return
            round_idx = len(record.data.rounds) + 1
            if round_idx > record.data.max_rounds:
                break
            try:
                evaluation = await self._run_round(
                    user_id,
                    discussion_id,
                    record,
                    round_idx,
                )
            except RoundFailure as exc:
                await self._fail(user_id, discussion_id, str(exc))
                return
            if evaluation is None:
                # The discussion vanished mid-round; nothing to drive.
                return
            if evaluation.consensus:
                break

        record = await self._storage.get_discussion(user_id, discussion_id)
        if record is None or record.data.status in _TERMINAL:
            return
        conclusion = await self._evaluation.synthesize(user_id, record)
        await self._mutate(user_id, discussion_id, _complete(conclusion))

    async def _run_round(
        self,
        user_id: str,
        discussion_id: str,
        record: DiscussionRecord,
        round_idx: int,
    ) -> RoundEvaluation | None:
        """Run one round: every debater argues once, then the judge.

        Debaters are strictly sequential — each sees the round's
        earlier speakers only through their rendered arguments in the
        prompt, while everything a debater itself has said rides its
        own session history (one session per debater across rounds).

        The round persists incrementally, every step through
        :meth:`_mutate`: an empty :class:`RoundEvaluation` lands before
        the first turn, each debater's argument lands as delivered
        (``current_speaker`` naming who holds the floor, and the judge
        while she verdicts), and the verdict folds onto that same
        round in place. A crash or failure mid-round therefore leaves
        the round durable exactly as far as it was argued.

        Args:
            user_id (`str`): The discussion's owner.
            discussion_id (`str`): The discussion being argued.
            record (`DiscussionRecord`): The discussion being argued.
            round_idx (`int`): Which round, 1-based.

        Returns:
            `RoundEvaluation | None`: The judged round, or ``None``
                when the discussion vanished mid-round.

        Raises:
            `RoundFailure`: A debater turn failed — the message names
                the round and the debater.
        """
        opened = await self._mutate(
            user_id,
            discussion_id,
            _open_round(round_idx),
        )
        if opened is None:
            return None
        arguments: list[RoundArgument] = []
        for debater in record.data.debaters:
            announced = await self._mutate(
                user_id,
                discussion_id,
                _set_speaker(debater.name),
            )
            if announced is None:
                return None
            text = await self._debater_turn(
                user_id,
                record,
                debater,
                round_idx,
            )
            argument = RoundArgument(debater=debater.name, text=text)
            arguments.append(argument)
            appended = await self._mutate(
                user_id,
                discussion_id,
                _append_argument(round_idx, argument),
            )
            if appended is None:
                return None
        judging = await self._mutate(
            user_id,
            discussion_id,
            _set_speaker(JUDGE_KEY),
        )
        if judging is None:
            return None
        evaluation = await self._evaluation.evaluate_round(
            user_id,
            record,
            round_idx,
            arguments,
        )
        judged = await self._mutate(
            user_id,
            discussion_id,
            _apply_evaluation(round_idx, evaluation),
        )
        if judged is None:
            return None
        return evaluation

    async def _debater_turn(
        self,
        user_id: str,
        record: DiscussionRecord,
        debater: DebaterConfig,
        round_idx: int,
    ) -> str:
        """One debater's turn in one round; the round-failure boundary.

        Everything a turn can do wrong — a chat runner that raises, a
        reply that closed with an error — becomes one :class:`RoundFailure`
        naming the round and the debater, so the completion body fails
        the discussion with a diagnosable error.
        """
        session_id, agent_id = await self._sessions.ensure(
            user_id,
            record,
            debater.name,
            debater.system_prompt,
            debater.chat_model_config,
        )
        prompt = _round_prompt(record, debater, round_idx)
        try:
            await run_turn(
                self._chat_runner,
                user_id,
                session_id,
                agent_id,
                prompt,
            )
            # ``who`` stays nameless: the RoundFailure below already
            # names the debater, and the detail must not repeat it.
            return await read_turn_reply(
                self._storage,
                user_id,
                session_id,
                "debater",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            raise RoundFailure(
                f"round {round_idx}: debater {debater.name!r} turn "
                f"failed: {exc}",
            ) from exc

    async def cancel(
        self,
        user_id: str,
        discussion_id: str,
    ) -> DiscussionRecord:
        """Cancel a discussion: stop its loop, mark the record.

        Args:
            user_id (`str`): The discussion's owner.
            discussion_id (`str`): The discussion to cancel.

        Returns:
            `DiscussionRecord`: The persisted, cancelled record.

        Raises:
            `HTTPException`: 404 when the discussion is missing.
        """
        record = await self._storage.get_discussion(user_id, discussion_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Discussion {discussion_id!r} not found.",
            )
        task = self._tasks.get(discussion_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pylint: disable=broad-except
                # A loop that died of something else mid-cancellation
                # must not mask the cancel the caller asked for.
                logger.error(
                    "Discussion %r loop raised while being cancelled.",
                    discussion_id,
                    exc_info=True,
                )
        # Reloaded after the loop settled: its final persists count.
        persisted = await self._mutate(user_id, discussion_id, _cancel)
        if persisted is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Discussion {discussion_id!r} not found.",
            )
        return persisted

    async def close(self) -> None:
        """Stop the engine: cancel every live debate loop.

        Storage must never close underneath a running loop, so the
        lifespan calls this before tearing the stack down. Records are
        deliberately left as they are — a debate mid-flight stays
        RUNNING, exactly what a crashed process leaves behind and what
        the next process's :meth:`recover_stale` fails out.
        """
        self._closed = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover_stale(self) -> int:
        """Fail out discussions a previous process left mid-flight.

        Redis has no global RUNNING query for free, so the storage
        layer keeps a small global active index (the workflow-run
        pattern) and this sweep reads it: every still-PENDING or
        still-RUNNING discussion belonged to a process that died — the
        loop task registry of this engine is empty at boot — and is
        marked FAILED with ``interrupted by restart``. A v1 discussion
        is not resumed: its participants' sessions are process-local,
        so an honest failure beats a half-replayed debate. Each record
        is reconciled in isolation: one unreadable or failing record is
        logged and skipped, never aborting the sweep (or the boot that
        runs it).

        Returns:
            `int`: How many discussions the sweep rewrote.
        """
        touched = 0
        for record in await self._storage.list_active_discussions():
            live = self._tasks.get(record.id)
            if live is not None and not live.done():
                # This process is driving it; not stale.
                continue
            try:
                persisted = await self._mutate(
                    record.user_id,
                    record.id,
                    _interrupted,
                )
            except Exception:  # pylint: disable=broad-except
                logger.error(
                    "Recovering discussion %r failed; skipping it.",
                    record.id,
                    exc_info=True,
                )
                continue
            if persisted is not None:
                logger.warning(
                    "Discussion %r was interrupted by a restart; "
                    "failed out.",
                    record.id,
                )
                touched += 1
        return touched

    # ------------------------------------------------------------------
    # Transition plumbing
    # ------------------------------------------------------------------

    async def _fail(
        self,
        user_id: str,
        discussion_id: str,
        error: str,
    ) -> None:
        """Persist one FAILED transition, terminal statuses kept."""
        persisted = await self._mutate(
            user_id,
            discussion_id,
            _fail_transition(error),
        )
        if persisted is None:
            logger.warning(
                "Failing discussion %r found it gone.",
                discussion_id,
            )

    async def _mutate(
        self,
        user_id: str,
        discussion_id: str,
        apply: Callable[[DiscussionRecord], None],
    ) -> DiscussionRecord | None:
        """One persisted discussion transition, serialised on the
        write lock.

        The record is reloaded inside the lock so a transition never
        overwrites a concurrent write (a cancel racing a round persist);
        *apply* mutates the fresh record in place and the persist
        happens before anything reads it back.

        Args:
            user_id (`str`): The discussion's owner.
            discussion_id (`str`): The discussion to transition.
            apply (`Callable`): The mutation, run under the lock.

        Returns:
            `DiscussionRecord | None`: The persisted record, or None
                when the discussion no longer exists.
        """
        async with self._write_lock:
            record = await self._storage.get_discussion(
                user_id,
                discussion_id,
            )
            if record is None:
                return None
            apply(record)
            return await self._storage.upsert_discussion(user_id, record)


def _mark_running(record: DiscussionRecord) -> None:
    """Transition helper: PENDING → RUNNING."""
    record.data.status = DiscussionStatus.RUNNING


def _open_round(round_idx: int) -> Callable[[DiscussionRecord], None]:
    """Transition helper factory: persist an empty in-progress round.

    Terminal statuses are kept — a cancel that landed before the round
    opened is never resurrected by the round it never argued.
    """

    def open_round(record: DiscussionRecord) -> None:
        if record.data.status in _TERMINAL:
            return
        record.data.rounds.append(RoundEvaluation(round=round_idx))

    return open_round


def _set_speaker(speaker: str) -> Callable[[DiscussionRecord], None]:
    """Transition helper factory: announce who holds the floor."""

    def set_speaker(record: DiscussionRecord) -> None:
        if record.data.status in _TERMINAL:
            return
        record.data.current_speaker = speaker

    return set_speaker


def _append_argument(
    round_idx: int,
    argument: RoundArgument,
) -> Callable[[DiscussionRecord], None]:
    """Transition helper factory: land one delivered argument on its
    round."""

    def append_argument(record: DiscussionRecord) -> None:
        if record.data.status in _TERMINAL:
            return
        in_progress = _live_round(record, round_idx)
        if in_progress is None:
            return
        in_progress.arguments.append(argument.model_copy())

    return append_argument


def _apply_evaluation(
    round_idx: int,
    evaluation: RoundEvaluation,
) -> Callable[[DiscussionRecord], None]:
    """Transition helper factory: fold a verdict onto its round.

    The live round's arguments are replaced with the evaluation's
    copies — same debaters, same texts, now carrying the judge's
    strengths, rebutted flags and notes — and the consensus and
    summary land with them.
    """

    def apply(record: DiscussionRecord) -> None:
        if record.data.status in _TERMINAL:
            return
        in_progress = _live_round(record, round_idx)
        if in_progress is None:
            return
        in_progress.arguments = [
            argument.model_copy() for argument in evaluation.arguments
        ]
        in_progress.consensus = evaluation.consensus
        in_progress.summary = evaluation.summary

    return apply


def _live_round(
    record: DiscussionRecord,
    round_idx: int,
) -> RoundEvaluation | None:
    """The round-*round_idx* slot of *record*, when it was opened.

    Rounds are opened in order and never removed, so the slot is the
    ``round_idx``-th; ``None`` means the round was never opened (a
    terminal status got there first) and writers step aside rather
    than touch a round that is not theirs.
    """
    if len(record.data.rounds) < round_idx:
        return None
    return record.data.rounds[round_idx - 1]


def _complete(conclusion: str) -> Callable[[DiscussionRecord], None]:
    """Transition helper factory: COMPLETED with the conclusion."""

    def complete(record: DiscussionRecord) -> None:
        if record.data.status in _TERMINAL:
            return
        record.data.status = DiscussionStatus.COMPLETED
        record.data.conclusion = conclusion
        record.data.current_speaker = None
        record.data.finished_at = datetime.now()

    return complete


def _cancel(record: DiscussionRecord) -> None:
    """Transition helper: CANCELLED, terminal statuses kept."""
    if record.data.status in _TERMINAL:
        return
    record.data.status = DiscussionStatus.CANCELLED
    record.data.current_speaker = None
    record.data.finished_at = datetime.now()


def _fail_transition(error: str) -> Callable[[DiscussionRecord], None]:
    """Transition helper factory: FAILED with the error.

    Every failure path lands here — a failed turn, the loop wrapper's
    crash handler — and all of them release the floor: a dead debate
    names no current speaker.
    """

    def fail(record: DiscussionRecord) -> None:
        if record.data.status in _TERMINAL:
            return
        record.data.status = DiscussionStatus.FAILED
        record.data.error = error
        record.data.current_speaker = None
        record.data.finished_at = datetime.now()

    return fail


def _interrupted(record: DiscussionRecord) -> None:
    """Transition helper: the stale sweep's FAILED, with its cause."""
    if record.data.status in _TERMINAL:
        return
    record.data.status = DiscussionStatus.FAILED
    record.data.error = _RESTART_ERROR
    record.data.current_speaker = None
    record.data.finished_at = datetime.now()


def _round_prompt(
    record: DiscussionRecord,
    debater: DebaterConfig,
    round_idx: int,
) -> str:
    """Render one debater's prompt for one round.

    Round 1 states the topic and asks for the debater's own case;
    every later round renders the OTHER debaters' latest arguments —
    with the judge's note on each, when the last round was evaluated —
    and asks for a rebuttal followed by a restated position. The
    debater's own prior arguments are not rendered: they already ride
    its session history. Wording stays domain-neutral; the domain
    lives in the user-supplied stances.

    Args:
        record (`DiscussionRecord`): The discussion being argued.
        debater (`DebaterConfig`): The debater being prompted.
        round_idx (`int`): Which round, 1-based.

    Returns:
        `str`: The round's user message.
    """
    head = f"Topic: {record.data.topic}\n\n"
    if round_idx == 1:
        return (
            head
            + f"Round 1 of {record.data.max_rounds}. State your "
            "position and make the strongest case for it, as set out "
            "in your instructions."
        )
    others = [
        argument
        for argument in record.data.rounds[-1].arguments
        if argument.debater != debater.name
    ]
    if not others:
        return (
            head
            + f"Round {round_idx} of {record.data.max_rounds}. No other "
            "debater has argued yet. Deepen the case you made in the "
            "earlier rounds and answer its weakest points yourself."
        )
    lines = [
        f"Round {round_idx} of {record.data.max_rounds}. The other "
        f"debaters argued in round {round_idx - 1}:",
        "",
    ]
    for argument in others:
        note = f" (judge: {argument.note})" if argument.note else ""
        lines.append(f"- {argument.debater}: {argument.text}{note}")
    lines.append("")
    lines.append(
        "Rebut their strongest points, then restate and defend your "
        "own position.",
    )
    return head + "\n".join(lines)
