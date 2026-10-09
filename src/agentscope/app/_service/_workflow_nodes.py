# -*- coding: utf-8 -*-
"""One attempt of each workflow node type.

The engine owns persistence, retries and park/resume; a handler does
exactly one attempt and either returns its blackboard output or
raises.
"""
import json
import re
from typing import Any, Awaitable, Callable

from ..._utils._common import _generate_id
from ...message import ToolCallState, UserMsg
from ...state import AgentState
from ...types import ReplyFinishedReason

from ..storage import (
    AgentNodeConfig,
    ChatModelConfig,
    ConditionNodeConfig,
    HumanApprovalNodeConfig,
    LoopNodeConfig,
    SessionConfig,
    StorageBase,
    WorkflowNode,
    WorkflowOrigin,
    WorkflowRunRecord,
)
from ._workflow_template import (
    RenderError,
    render_template,
    resolve_path,
    scalar_text,
)


ChatRunner = Callable[..., Awaitable[None]]
"""Drives one agent turn to completion (``ChatService.run`` in prod)."""

JudgeAgentFactory = Callable[[str, str, str], Awaitable[str]]
"""(user_id, name, system_prompt) -> agent id for judge sessions."""


class NodeFailure(Exception):
    """One attempt of a node failed; message is user-facing."""


class ApprovalPending(Exception):
    """A node parks the run waiting for a human verdict.

    The engine catches it to mark the node WAITING_HUMAN rather than
    counting a failed attempt.
    """

    def __init__(self, node_id: str, question: str) -> None:
        super().__init__(f"Approval pending on node {node_id!r}.")
        self.node_id = node_id
        self.question = question


_SESSIONS_KEY = "_sessions"
"""Context key holding node session ids — engine bookkeeping."""

_JUDGE_RULE_PREFIX = "llm_judge:"

_PARKED_TOOL_STATES = (
    ToolCallState.PENDING,
    ToolCallState.ASKING,
    ToolCallState.SUBMITTED,
)
"""Block states meaning the turn parked on a tool-call decision.

``asking`` and ``submitted`` are what the chat and session services
treat as a parked turn; ``pending`` is belt-and-braces for a turn
that ended before the permission system even looked at the call.
``allowed`` and ``finished`` mean the turn went on, so they stay
unflagged."""


class NodeHandlers:
    """The four node behaviours a workflow run dispatches to.

    Handlers never persist the run: they stash what a retry needs
    (session ids) into ``run.data.context`` in memory and return the
    node's blackboard output, leaving persistence, retries and
    park/resume to the engine.
    """

    def __init__(
        self,
        storage: StorageBase,
        chat_runner: ChatRunner,
        judge_agent_factory: JudgeAgentFactory,
    ) -> None:
        """Initialize the handler set.

        Args:
            storage (`StorageBase`): Application storage.
            chat_runner (`ChatRunner`): Drives one agent turn to
                completion (``ChatService.run`` in production).
            judge_agent_factory (`JudgeAgentFactory`): Ensures the
                shared generic judge agent exists and returns its id;
                the specific instruction always rides the user
                message, never the system prompt.
        """
        self._storage = storage
        self._chat_runner = chat_runner
        self._judge_agent_factory = judge_agent_factory
        self._handlers: dict[
            str,
            Callable[
                [str, WorkflowRunRecord, WorkflowNode],
                Awaitable[dict[str, Any]],
            ],
        ] = {
            "agent": self.handle_agent,
            "condition": self.handle_condition,
            "loop": self.handle_loop,
            "human_approval": self.handle_approval,
        }

    async def handle(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        node: WorkflowNode,
    ) -> dict[str, Any]:
        """Dispatch one attempt of *node* to its handler.

        Args:
            user_id (`str`): The run's owner.
            run (`WorkflowRunRecord`): The run the node belongs to.
            node (`WorkflowNode`): The node to attempt.

        Returns:
            `dict[str, Any]`: The node's blackboard output.

        Raises:
            `NodeFailure`: The attempt failed, the config type is
                unknown to this handler set, or an unexpected error
                (storage, judge factory, chat runner) interrupted
                the attempt.
        """
        handler = self._handlers.get(node.config.type)
        if handler is None:
            raise NodeFailure(
                f"Node {node.id!r} has unknown config type "
                f"{node.config.type!r}.",
            )
        try:
            return await handler(user_id, run, node)
        except (NodeFailure, ApprovalPending):
            # The handlers' own vocabulary: reported as a failed
            # attempt or parked, never re-wrapped.
            raise
        except Exception as exc:
            # Storage, the judge factory or the chat runner blew
            # up: one failed attempt, not a broken engine.
            # CancelledError is a BaseException and passes through.
            raise NodeFailure(str(exc)) from exc

    async def handle_agent(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        node: WorkflowNode,
    ) -> dict[str, Any]:
        """One agent turn: ensure a session, prompt it, read the reply.

        The session id is stashed into ``run.data.context`` in memory
        only — the engine persists the run once this returns, which is
        what lets a retry rejoin the same conversation.
        """
        cfg = node.config
        if not isinstance(cfg, AgentNodeConfig):
            # Unreachable via ``handle``; narrows the config union.
            raise NodeFailure(f"Node {node.id!r} is not an agent node.")

        agent = await self._storage.get_agent(user_id, cfg.agent_id)
        if agent is None:
            # Recorded debt: a deleted template fails the node like
            # any other attempt, under the engine's retry budget.
            raise NodeFailure(
                f"Node {node.id!r}: agent {cfg.agent_id!r} no longer "
                "exists.",
            )

        model = agent.data.chat_config.chat_model_config
        if model is None:
            model = run.data.chat_model_config
        if model is None:
            raise NodeFailure(
                f"Node {node.id!r} has no model to run with: set one on "
                f"agent {cfg.agent_id!r} or pass a run-level default "
                "model.",
            )

        session_id = await self._ensure_session(
            user_id,
            run,
            node.id,
            cfg.agent_id,
            model,
        )
        await self._chat_runner(
            user_id,
            session_id,
            cfg.agent_id,
            UserMsg(
                name="workflow",
                content=_render(
                    cfg.prompt_template,
                    _blackboard(run),
                ),
            ),
        )
        return await self._read_reply(
            user_id,
            node.id,
            cfg.agent_id,
            session_id,
        )

    async def handle_condition(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        node: WorkflowNode,
    ) -> dict[str, Any]:
        """Pick one branch: rules first, then an LLM judge.

        Rules are tried in declaration order; ``llm_judge:`` rules are
        skipped by that first pass. When nothing matches, a generic
        judge agent — its system prompt only lists the labels — is
        asked through the user message, which carries the specific
        instruction plus a JSON dump of the blackboard. When several
        ``llm_judge`` arms exist, the first one's instruction is the
        one asked.
        """
        cfg = node.config
        if not isinstance(cfg, ConditionNodeConfig):
            raise NodeFailure(f"Node {node.id!r} is not a condition node.")

        blackboard = _blackboard(run)
        for branch in cfg.branches:
            if branch.rule.startswith(_JUDGE_RULE_PREFIX):
                continue
            if _rule_matches(branch.rule, blackboard):
                return {"branch": branch.label}

        labels = [branch.label for branch in cfg.branches]
        instruction = next(
            (
                branch.rule[len(_JUDGE_RULE_PREFIX):].strip()
                for branch in cfg.branches
                if branch.rule.startswith(_JUDGE_RULE_PREFIX)
            ),
            "Pick the branch that applies.",
        )
        model = cfg.chat_model_config or run.data.chat_model_config
        if model is None:
            raise NodeFailure(
                f"Node {node.id!r} has no model for its llm_judge rule: "
                "set one on the node or pass a run-level default model.",
            )
        judge_id = await self._judge_agent_factory(
            user_id,
            "workflow-branch-judge",
            "You are a routing judge for a workflow. Reply with "
            "exactly one of these branch labels and nothing else: "
            f"{', '.join(labels)}.",
        )
        session_id = await self._ensure_session(
            user_id,
            run,
            f"{node.id}:judge",
            judge_id,
            model,
        )
        await self._chat_runner(
            user_id,
            session_id,
            judge_id,
            UserMsg(
                name="workflow",
                content=(
                    f"{instruction}\n\nBlackboard:\n"
                    f"{json.dumps(blackboard, ensure_ascii=False)}"
                ),
            ),
        )
        reply = await self._read_reply(
            user_id,
            node.id,
            judge_id,
            session_id,
        )
        label = _match_label(reply["text"] or "", labels)
        if label is None:
            raise NodeFailure(
                f"Node {node.id!r}: judge reply matched no branch label: "
                f"{reply['text']!r}.",
            )
        return {"branch": label}

    async def handle_loop(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        node: WorkflowNode,
    ) -> dict[str, Any]:
        """One attempt of a loop node.

        ``iterate`` walks the rendered list strictly sequentially —
        ``parallel`` is parsed by the model but v1 ignores it. Each
        item gets its own body session, keyed by position, so a retry
        re-runs only that item's conversation.

        ``retry_until`` is ONE attempt: one body turn plus one
        verifier verdict. The body session keeps a single key, so
        engine-driven retries append into the same conversation — SOP
        retry semantics. The engine counts attempts and enforces
        ``max_attempts``.
        """
        cfg = node.config
        if not isinstance(cfg, LoopNodeConfig):
            raise NodeFailure(f"Node {node.id!r} is not a loop node.")

        if cfg.mode == "iterate":
            return await self._iterate(user_id, run, node, cfg)

        body = await self._run_body_once(
            user_id,
            run,
            f"{node.id}:body",
            cfg.body_agent_id,
            _render(cfg.item_template, _blackboard(run)),
        )
        verdict = await self._run_verifier(user_id, run, node, body)
        if verdict != "PASS":
            raise NodeFailure(
                f"Node {node.id!r}: verifier rejected the attempt: "
                f"{scalar_text(body.get('text'))[:200]!r}.",
            )
        return {"items": [body], "attempts": 1}

    async def _iterate(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        node: WorkflowNode,
        cfg: LoopNodeConfig,
    ) -> dict[str, Any]:
        """The ``iterate`` half of :meth:`handle_loop`."""
        raw = _render(cfg.list_template, _blackboard(run))
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NodeFailure(
                f"Node {node.id!r}: list_template did not render to "
                f"valid JSON: {exc}.",
            ) from exc
        if not isinstance(items, list):
            raise NodeFailure(
                f"Node {node.id!r}: list_template must render to a JSON "
                f"list, got {type(items).__name__}.",
            )
        if items and not cfg.body_agent_id:
            raise NodeFailure(
                f"Node {node.id!r} has no body agent to iterate with: "
                "set body_agent_id on the loop node.",
            )
        blackboard = _blackboard(run)
        outputs: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            outputs.append(
                await self._run_body_once(
                    user_id,
                    run,
                    f"{node.id}:body:{index}",
                    cfg.body_agent_id,
                    _render(
                        cfg.item_template,
                        blackboard | {"item": item},
                    ),
                ),
            )
        return {"items": outputs, "attempts": len(items)}

    async def handle_approval(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        node: WorkflowNode,
    ) -> dict[str, Any]:
        """Park the run for a human verdict; never returns normally.

        v1: the run owner verdicts; ``approvers`` is only recorded in
        the question the approver UI will show.
        """
        cfg = node.config
        if not isinstance(cfg, HumanApprovalNodeConfig):
            raise NodeFailure(f"Node {node.id!r} is not an approval node.")
        question = (
            f"Workflow '{run.data.workflow_name}' is waiting on node "
            f"'{node.id}': review the run so far and approve it to go "
            "on, or reject it to stop."
        )
        if cfg.approvers:
            question += (
                f" Awaiting approval from: {', '.join(cfg.approvers)}."
            )
        raise ApprovalPending(node.id, question)

    async def _run_body_once(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        key: str,
        agent_id: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Run one loop body turn and read back its reply.

        The session key comes from the caller: ``iterate`` indexes it
        by item position so retries do not double-append into another
        item's conversation, while ``retry_until`` keeps one key so
        attempts share a thread.
        """
        agent = await self._storage.get_agent(user_id, agent_id)
        if agent is None:
            # The key is node-scoped (``<node>:body[:<index>]``), so it
            # names the node without threading another parameter.
            raise NodeFailure(
                f"Node {key!r}: agent {agent_id!r} no longer exists.",
            )
        model = agent.data.chat_config.chat_model_config
        if model is None:
            model = run.data.chat_model_config
        if model is None:
            raise NodeFailure(
                f"Node {key!r}: loop body agent {agent_id!r} has no "
                "model: set one on the agent or pass a run-level "
                "default model.",
            )
        session_id = await self._ensure_session(
            user_id,
            run,
            key,
            agent_id,
            model,
        )
        await self._chat_runner(
            user_id,
            session_id,
            agent_id,
            UserMsg(name="workflow", content=prompt),
        )
        return await self._read_reply(user_id, key, agent_id, session_id)

    async def _run_verifier(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        node: WorkflowNode,
        body: dict[str, Any],
    ) -> str:
        """Ask the judge agent for a PASS/FAIL verdict on the body."""
        cfg = node.config
        if not isinstance(cfg, LoopNodeConfig):
            raise NodeFailure(f"Node {node.id!r} is not a loop node.")
        model = run.data.chat_model_config
        if model is None:
            # Loops carry no model field of their own, so only a
            # run-level default can drive the verifier.
            raise NodeFailure(
                f"Node {node.id!r} has no model for its verifier: pass "
                "a run-level default model for the run.",
            )
        judge_id = await self._judge_agent_factory(
            user_id,
            "workflow-verifier",
            "You are a verifier for a workflow. Reply with exactly "
            "PASS when the work meets the criteria the user message "
            "holds, exactly FAIL when it does not, and nothing else.",
        )
        session_id = await self._ensure_session(
            user_id,
            run,
            f"{node.id}:verifier",
            judge_id,
            model,
        )
        await self._chat_runner(
            user_id,
            session_id,
            judge_id,
            UserMsg(
                name="workflow",
                content=(
                    f"Criteria:\n{cfg.verifier_prompt}\n\n"
                    f"Work to verify:\n{scalar_text(body.get('text'))}"
                ),
            ),
        )
        reply = await self._read_reply(
            user_id,
            node.id,
            judge_id,
            session_id,
        )
        verdict = _match_label(reply["text"] or "", ["PASS", "FAIL"])
        if verdict is None:
            raise NodeFailure(
                f"Node {node.id!r}: verifier reply was neither PASS nor "
                f"FAIL: {reply['text']!r}.",
            )
        return verdict

    async def _ensure_session(
        self,
        user_id: str,
        run: WorkflowRunRecord,
        key: str,
        agent_id: str,
        model: ChatModelConfig,
    ) -> str:
        """Return the session id for *key*, creating it on first use.

        Ids are stashed into ``run.data.context["_sessions"]`` in
        memory only: the engine persists the run once the handler
        returns, which is what makes a retry rejoin the same
        conversation. A cached id is reused without a storage
        roundtrip — a session deleted under a live run surfaces as a
        failed chat turn under the engine's retry budget.
        """
        sessions = run.data.context.setdefault(_SESSIONS_KEY, {})
        if isinstance(sessions, dict) and key in sessions:
            return str(sessions[key])

        session = await self._storage.upsert_session(
            user_id=user_id,
            agent_id=agent_id,
            config=SessionConfig(
                workspace_id=_generate_id(),
                name=f"{run.data.workflow_name} / {key}",
                chat_model_config=model,
                fallback_chat_model_config=None,
            ),
            state=AgentState(),
            origin=WorkflowOrigin(
                workflow_run_id=run.id,
                node_id=key,
            ),
        )
        sessions[key] = session.id
        return session.id

    async def _read_reply(
        self,
        user_id: str,
        node_id: str,
        agent_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Read the attempt's final reply: the last assistant message.

        ``list_messages`` returns a page in chronological order, so
        the run's final reply is the last assistant message in it.
        That message also decides the attempt's health: prod
        ``ChatService.run`` swallows its exceptions and closes the
        reply with an error or a non-COMPLETED reason instead of
        raising, so an error-bearing turn must fail the node rather
        than feed its partial text downstream.
        """
        messages, _ = await self._storage.list_messages(
            user_id,
            session_id,
            limit=20,
        )
        for message in reversed(messages):
            if message.role != "assistant":
                continue
            if message.error is not None:
                raise NodeFailure(
                    f"Node {node_id!r}: agent {agent_id!r} turn "
                    f"failed: {message.error.message} "
                    f"({message.error.type}).",
                )
            if message.finished_reason not in (
                None,
                ReplyFinishedReason.COMPLETED,
            ):
                raise NodeFailure(
                    f"Node {node_id!r}: agent {agent_id!r} turn "
                    f"ended as {message.finished_reason.value!r}.",
                )
            if any(
                block.state in _PARKED_TOOL_STATES
                for block in message.get_content_blocks("tool_call")
            ):
                raise NodeFailure(
                    f"Node {node_id!r}: agent {agent_id!r} turn ended "
                    "with a tool call still awaiting a decision.",
                )
            return {
                "text": message.get_text_content() or "",
                "structured": message.structured_output,
            }
        raise NodeFailure(
            f"Node {node_id!r}: agent {agent_id!r} ended its turn "
            f"without a reply in session {session_id!r}.",
        )


def _blackboard(run: WorkflowRunRecord) -> dict[str, Any]:
    """The template and rule view of a run.

    ``input`` is the run's input; ``nodes`` wraps each stored node
    output under an ``output`` key, so templates and rules spell them
    ``nodes.<id>.output.<field>`` — the shape the config descriptions
    and the renderer tests use. Keys starting with ``_`` are engine
    bookkeeping (the session-id stash) and never reach a prompt.
    """
    return {
        "input": run.data.input,
        "nodes": {
            node_id: {"output": output}
            for node_id, output in run.data.context.items()
            if not node_id.startswith("_")
        },
    }


def _render(template: str, context: dict[str, Any]) -> str:
    """Render, translating a bad path into a node failure."""
    try:
        return render_template(template, context)
    except RenderError as exc:
        raise NodeFailure(str(exc)) from exc


def _rule_matches(rule: str, blackboard: dict[str, Any]) -> bool:
    """Whether ``<dotted.path> == <literal>`` holds on the blackboard.

    Literals: ``true``/``false``/``null``, integers, floats,
    ``"quoted"``/``'quoted'`` strings (exact) and bare words (matched
    case-insensitively against the value as a template renders it).
    A path the blackboard cannot resolve — or a rule without ``==`` —
    simply does not match, leaving the fall-through judge to decide.
    """
    path, sep, literal = rule.partition("==")
    if not sep:
        return False
    try:
        resolved = resolve_path(path.strip(), blackboard)
    except RenderError:
        return False
    literal = literal.strip()
    quoted = (
        len(literal) >= 2
        and literal[0] == literal[-1]
        and literal[0] in ("\"", "'")
    )
    return _operands_equal(resolved, _parse_literal(literal), not quoted)


def _parse_literal(text: str) -> Any:
    """Parse a rule literal into a bool, None, number or string.

    Quoted literals (``"yes"`` / ``'yes'``) keep their contents as an
    exact-match string; anything else that is not a keyword or a
    number stays a bare word for case-insensitive matching.
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _operands_equal(resolved: Any, expected: Any, fold: bool) -> bool:
    """Compare a resolved value with a parsed rule literal.

    ``fold`` marks a bare-word literal, which matches the value the
    way a template renders it, case-insensitively; quoted literals
    match strings exactly.
    """
    if expected is None:
        return resolved is None
    if isinstance(expected, bool):
        return isinstance(resolved, bool) and resolved == expected
    if isinstance(expected, (int, float)):
        return (
            isinstance(resolved, (int, float))
            and not isinstance(resolved, bool)
            and resolved == expected
        )
    if fold:
        return scalar_text(resolved).lower() == expected.lower()
    return isinstance(resolved, str) and resolved == expected


def _match_label(text: str, labels: list[str]) -> str | None:
    """The first label appearing in *text* as a whole word.

    Matching is case-insensitive and honours word boundaries, so
    ``unpassable`` does not count as ``PASS`` while formatting such
    as ``**PASS**.`` still matches. Labels keep their declaration
    order — a reply holding two of them picks the first listed.
    """
    lowered = text.lower()
    for label in labels:
        if re.search(
            rf"(?<![0-9a-z_]){re.escape(label.lower())}(?![0-9a-z_])",
            lowered,
        ):
            return label
    return None
