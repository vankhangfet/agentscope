# Software Factory — Design (v1)

- **Date:** 2026-10-18
- **Status:** Approved (user blueprint: 5 role groups, discussion-challenge core, SDLC phases, modes B→C first)
- **Goal:** A human-in-the-loop multi-agent software factory: a ticket (the human's problem statement) flows through Requirements → Architecture → Develop phases; each phase runs multi-agent discussion (reuse the Discussion capability), humans approve at gates, and development tasks execute agent turns in a per-ticket workspace.

## Core concepts

- **Logical roles** are data — debater configs (name + stance prompt), many sharing one model (the ticket's model). A role catalog module ships default groups per phase:
  - Requirements group: Product Owner, Business Analyst, Requirement Reviewer
  - Architecture group: Solution Architect, Cloud Architect, Performance Reviewer, Security Reviewer, Cost Reviewer, Design Reviewer
  - Development roles: Plan Lead (task breakdown), Developer, Code Reviewer
- **Ticket** = the unit: title, description (human's problem), mode, phase, artifacts.
- **Phases**: `REQUIREMENTS` → `ARCHITECTURE` → `DEVELOP`. Phases 1–2 run a Discussion with the phase's role group (human can follow live in the Discussions page). Phase 3 runs agent turns (Plan Lead → tasks → Developer per task → Reviewer) in the ticket workspace.
- **Gates**: after each phase the ticket parks `WAITING_HUMAN`; approve(+note) advances, reject(+note) re-runs the current phase once (returns to the phase's start). In `DELEGATED` mode gates auto-approve except the final one.
- **No forced consensus**: a discussion ending without consensus still produces artifacts; the gate view shows the disagreement — the human decides.
- **Decision log**: each discussion's conclusion + the human's approve note + risks noted land in the ticket record (`decisions` list).

## Data model (`storage/_model/_factory.py`)

```python
class FactoryMode(StrEnum): COLLABORATIVE, DELEGATED
class FactoryPhase(StrEnum): REQUIREMENTS, ARCHITECTURE, DEVELOP
class FactoryStatus(StrEnum):
    PENDING, RUNNING, WAITING_HUMAN, COMPLETED, FAILED, CANCELLED

class FactoryTask(BaseModel):
    title: str; description: str = ""
    assignee: str = ""              # logical role name
    status: Literal[PENDING, RUNNING, DONE, FAILED] = PENDING
    output: str | None = None

class FactoryDecision(BaseModel):
    phase: FactoryPhase; discussion_id: str | None
    conclusion: str; note: str = ""              # human gate note
    consensus: bool; created_at

class FactoryTicketData(BaseModel):
    title, description: str
    mode: FactoryMode = COLLABORATIVE
    phase: FactoryPhase = REQUIREMENTS
    status: FactoryStatus = PENDING
    chat_model_config: ChatModelConfig             # every role's model (v1: one per ticket)
    discussions: dict[FactoryPhase, str]           # phase -> discussion id
    requirements: str | None                       # phase 1 artifact
    architecture: str | None                       # phase 2 artifact
    tasks: list[FactoryTask] = []                  # phase 3
    review: str | None                             # reviewer output
    decisions: list[FactoryDecision] = []
    workspace_id: str | None                       # minted at DEVELOP
    error: str | None; trigger = "manual"; finished_at
class FactoryTicketRecord(_RecordBase): user_id + data
```

Storage on both backends + migration `0008_factory_tickets`, global active index (mirror discussions' `discussion_active_index`) for the stale sweep.

## Role catalog (`_service/factory/_roles.py`)

Pure data: `REQUIREMENTS_ROLES`, `ARCHITECTURE_ROLES`, plus PL/DEVELOPER/REVIEWER stance prompts. Each entry: `(name, stance system prompt)`. Prompts are domain-neutral English. `to_debaters(group, model)` builds `DebaterConfig`s.

## FactoryEngine (`_service/factory/_engine.py`)

- `dispatch/cancel/close/recover_stale` — the established background-task pattern (zero-await dispatch, registry, lifespan close, boot sweep FAILED "interrupted by restart").
- `_run_to_completion(user_id, ticket_id)`:
  1. Phase REQUIREMENTS: create a Discussion (requirements group, 2 rounds default) → await its completion (DebateEngine gains a small public `run_to_completion(user_id, discussion_id)` wrapper — used by the factory and by tests) → requirements = conclusion → log decision → gate.
  2. Phase ARCHITECTURE: same with the architecture group → architecture artifact + decision → gate.
  3. Phase DEVELOP: mint workspace_id → Plan Lead turn (session in ticket workspace, workspace tools available) → parse task list JSON (brace extractor, retry-once, fallback single-task) → Developer turn per task (same session? one session per task index; sequential) → Reviewer turn → review artifact → final gate (always human).
- Gates: `_gate()` sets `WAITING_HUMAN` (persist); `approve(user, id, note)` advances (auto-approve skipped in DELEGATED except final gate); `reject(user, id, note)` re-runs the phase (discussions of that phase are replaced by a fresh one).
- Every transition via `_mutate` (write lock + reload + terminal guards), mirroring DebateEngine.

## API (`_router/_factory.py`)

```
POST /factory            {title, description, chat_model_config, mode?} → 201 {ticket_id}
GET  /factory?status=    newest first
GET  /factory/{id}       → {data}
POST /factory/{id}/approve   {note?}   (404 / 409 when not WAITING_HUMAN)
POST /factory/{id}/reject    {note?}   (re-runs current phase)
POST /factory/{id}/cancel
```

## Frontend

- Sidebar **Factory** (icon `Factory`, after Discussions). List: title, phase chip, status chip, mode, updated; New Ticket dialog: title, description textarea, model (LlmSelect, required), mode toggle (Collaborative/Delegated); poll 5s while non-terminal.
- Ticket detail: phase timeline (3 phases + gates), status chip, mode, per-phase artifact panels (requirements text, architecture text + decision log, tasks list with status, review), links to each phase's discussion (navigate to Discussions detail), approve/reject bar with note input when `WAITING_HUMAN`, cancel otherwise. i18n en/zh.

## Non-goals (v1)

- Modes A and D (assist / bounded autonomy); per-role models; git push / MR creation (artifacts stay in the ticket workspace); drag Kanban; auto-deployment.

## Plan (6 tasks)

1. Models + storage + migration 0008
2. Role catalog + DebateEngine `run_to_completion` public wrapper
3. FactoryEngine (phases, gates, modes, recovery)
4. REST endpoints + lifespan wiring
5. Frontend (menu, list, create, detail with gates)
6. Sweep + final review + merge
