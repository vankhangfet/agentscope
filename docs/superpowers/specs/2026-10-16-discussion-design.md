# Discussion — Multi-Agent Debate Capability Design

- **Date:** 2026-10-16
- **Status:** Approved (N configurable debaters; background + live UI)
- **Goal:** A domain-agnostic debate capability: N configurable debaters argue an issue over rounds; an evaluator judges arguments, detects consensus, and synthesizes a conclusion. Reused by any agent domain (PM, AWS architecture, code review, security) without code changes.

## Three separated components (per requirement)

1. **`discussion()` — public API facade** (`src/agentscope/app/discussion/__init__.py`): creates the record, validates configs, dispatches the engine; returns the record. Thin, stable surface callers depend on.
2. **`DebateEngine`** (`_service/discussion/_debate.py`): drives rounds — round r: each debater argues (seeing prior rounds' arguments + evaluations), state persisted before/after every transition; stop conditions = `max_rounds` reached, early consensus (evaluator verdict), cancel, crash-safe resume on boot.
3. **`EvaluationEngine`** (`_service/discussion/_evaluation.py`): after each round — verifies arguments (strength score + rebuttal-check per debater, via the judge agent), detects consensus; at stop — synthesizes the final conclusion with per-debater positions.

Domain neutrality: debaters are data (name, stance/system prompt, model), not code. Judge is data too (prompt + model). Changing domain = changing payloads.

## Data model (`storage/_model/_discussion.py`)

```python
class DebaterConfig(BaseModel):
    name: str
    system_prompt: str          # the stance / domain expertise
    chat_model_config: ChatModelConfig

class JudgeConfig(BaseModel):
    system_prompt: str = "You are an impartial debate judge..."
    chat_model_config: ChatModelConfig

class RoundArgument(BaseModel):
    debater: str
    text: str
    # evaluation fills in:
    strength: int | None = None        # 1-5
    rebutted: bool | None = None       # a later argument beats it
    note: str = ""                      # judge's one-line assessment

class RoundEvaluation(BaseModel):
    round: int
    arguments: list[RoundArgument]
    consensus: bool = False
    summary: str = ""                   # judge's round summary

class DiscussionData(BaseModel):
    topic: str
    status: Literal[PENDING, RUNNING, COMPLETED, FAILED, CANCELLED]
    max_rounds: int = 3                  # 1..10, user-configurable
    debaters: list[DebaterConfig]        # 1..6
    judge: JudgeConfig
    rounds: list[RoundEvaluation]        # blackboard
    conclusion: str | None = None        # final synthesis
    error: str | None
    trigger: str = "manual"
    finished_at: datetime | None

class DiscussionRecord(_RecordBase): user_id + data
```

Storage on both backends + migration `0007_discussions` (mirror the workflow-run pattern: active index not needed — discussions are bounded and short; list by user newest-first).

## Execution

- Sessions per debater + judge via the workflow-node pattern: `upsert_session` with `DiscussionOrigin(workflow-style: discussion_id, debater)`; turns via `ChatService.run` (same proven path).
- Engine = background task per discussion (registry, cancel, lifespan close, boot-time stale sweep marking stale RUNNING → FAILED with "interrupted by restart", resumable by re-dispatch). Persist every transition first (crash-safe, mirroring WorkflowEngine).
- Round flow: round r → debaters argue sequentially (bounded; v1 sequential — debaters see each other within the round) → EvaluationEngine judges → persist round → check stop (max_rounds / consensus) → next or synthesize conclusion.

## API (`_router/_discussion.py`)

```
POST /discussion               {topic, debaters[1..6], judge?, max_rounds 1..10} → 201 {discussion_id}
GET  /discussion?status=       list, newest first
GET  /discussion/{id}          full record
POST /discussion/{id}/cancel   CANCELLED + task cancelled
```

## Frontend

- Sidebar menu **Discussions** (panel trái) — page `/discussion`: list (topic, status chip, rounds count, updated) + **New Discussion** dialog: topic textarea; debaters dynamic rows (name, stance textarea, model via LlmSelect — add/remove, 1-6); judge section (prompt + model); max_rounds number input (1-10).
- Detail `/discussion/{id}`: header (status, cancel), rounds timeline — per round: debater argument cards (strength stars, rebutted strike, judge note), round summary + consensus chip; conclusion panel. Poll 5s while non-terminal. i18n en/zh.

## Non-goals (v1)

- SSE live stream (polling v1; SSE follows the workflow-run pattern later)
- Parallel debater turns within a round; human participation mid-debate
- Streaming argument text into the UI while a debater writes
