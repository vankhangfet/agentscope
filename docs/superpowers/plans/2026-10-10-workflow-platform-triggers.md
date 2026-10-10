# Workflow Platform — Triggers Implementation Plan (Plan 3 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the spec's four trigger modes: manual UI run (done in Plan 2), scheduled runs (cron → engine), chat-invoked runs (`run_workflow` tool), plus `POST /credential/test`; fold in the recorded hot-loop performance notes.

**Architecture:** Reuse the existing APScheduler-backed `SchedulerManager` by letting a `ScheduleRecord` target a WORKFLOW instead of an agent (one new optional field pair, one branch in `_trigger`). The chat tool follows the established `ToolBase` pattern (see the scheduler's own tools) and is registered in the per-turn toolkit. The credential test is a thin endpoint over the existing model-resolution path.

**Tech Stack:** Python 3.11, FastAPI, APScheduler (existing), pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-workflow-platform-design.md` (sections 6-7)
**Prior plans:** backend foundation (Plan 1), engine (Plan 2 — merged). Recorded inputs folded in: perf notes (Redis pipeline / mget / run-list status filter).

**All code, identifiers, comments in English. Run tests:** `.venv/Scripts/python.exe -m pytest ...`
**Branch:** `workflow-triggers` (from `main`).

---

## File Structure

| File | Responsibility |
|---|---|
| Modify `src/agentscope/app/_router/_credential.py` + `_router/_schema/_credential.py` | `POST /credential/test` |
| Modify `src/agentscope/app/storage/_model/_schedule.py` | `ScheduleData.workflow_id`/`run_input` |
| Modify `src/agentscope/app/_manager/_scheduler/_scheduler_manager.py` | workflow branch in `_trigger` |
| Modify `src/agentscope/app/_router/_schedule.py` + `_router/_schema/_schedule.py` | accept workflow-target schedules |
| Create `src/agentscope/app/_tool/_workflow_run.py` | `WorkflowRun` chat tool |
| Modify `src/agentscope/app/_service/_toolkit.py`, `_service/_chat.py`, `_lifespan.py` | tool registration + engine wiring |
| Modify `src/agentscope/app/storage/_redis_storage.py` | pipeline + mget perf |
| Tests: `tests/credential_test_endpoint_test.py`, `tests/workflow_schedule_test.py`, `tests/workflow_run_tool_test.py` (new); `tests/workflow_run_storage_test.py` (extended in Task 4) | per-task suites |

---

### Task 1: `POST /credential/test`

**Files:**
- Modify: `src/agentscope/app/_router/_schema/_credential.py` — `TestCredentialRequest{model: str, parameters: dict = {}}` (plus optional `base_url`-free — credentials carry their own connection fields).
- Modify: `src/agentscope/app/_router/_credential.py` — endpoint:

```
POST /credential/{credential_id}/test   (200 TestCredentialResult)
```

Semantics: resolve the credential via the existing access service (owner-or-shared, mirroring the router's other endpoints); build the model via `_service._model.get_model(user_id, ChatModelConfig(type=credential.type-matching-shape, credential_id, model, parameters), access)`; await ONE minimal call — read how `ChatModelBase.__call__`/`acall` is invoked (a single `Msg` in, await the response — check `_service/_model.py`'s consumers for the canonical one-shot invocation, e.g. how SOP verifiers or tests call a model); catch everything → `{"ok": false, "detail": str(exc)}`; success → `{"ok": true, "detail": "model replied"}`. Never 500 on provider errors — the endpoint's job is to REPORT health.

**Tests** `tests/credential_test_endpoint_test.py`: full app + the fake-credential pattern from `tests/app_workflow_run_router_test.py` (register a test-only credential whose model replies instantly, and one whose model raises): ok=true path (assert the fake model actually got called with the chosen model name); provider failure → 200 `{ok: false}` with the error text; unknown credential → 404; another user's credential → 404.

- [ ] TDD → PASS → commit `feat(credential): test-connection endpoint`.

---

### Task 2: Scheduled workflow runs

**Files:**
- Modify: `src/agentscope/app/storage/_model/_schedule.py` — add to `ScheduleData`:

```python
    workflow_id: str | None = Field(
        default=None,
        description=(
            "Target workflow to run on each fire; when set, the "
            "schedule starts a workflow run instead of an agent turn."
        ),
    )

    run_input: dict[str, Any] = Field(
        default_factory=dict,
        description="Input handed to each workflow run the schedule starts.",
    )
```

(import `Any`; a schedule targets EITHER agent_id (existing semantics) OR workflow_id — keep `agent_id` required on the record as-is and make the SCHEDULER branch on `workflow_id` first; a workflow-target schedule ignores the agent/model fields — document in the field description. Add a `model_validator` requiring `run_input == {}` when `workflow_id is None`? NO — keep permissive, ignored.)

- Modify: `src/agentscope/app/_manager/_scheduler/_scheduler_manager.py` — in `_trigger`, BEFORE the agent-session branch:

```python
                if record.data.workflow_id is not None:
                    await self._fire_workflow(record)
                    return
```

`_fire_workflow`: load `WorkflowService`-equivalent behaviour — the manager holds `storage`; it needs start_run+dispatch. Wiring: `SchedulerManager.__init__` gains an optional `workflow_starter: Callable[[str, ScheduleRecord], Awaitable[None]]` (lifespan injects a closure `lambda user_id, record: (start_run(...) then dispatch(...))` — a closure avoids importing the engine into the manager and keeps the manager testable; trigger="schedule"). Errors: log-and-return (never let one fire kill the APScheduler job — mirror the existing try/except shape). If `workflow_starter` is None (older wiring/tests) → log a warning and skip.
- Modify `_lifespan.py`: build the closure from `workflow_service` + `workflow_engine` and pass it (find where SchedulerManager is constructed).
- Modify `src/agentscope/app/_router/_schedule.py` + `_schema/_schedule.py`: create/update accept the two new optional fields (mirror existing schema style; validation: workflow must exist when `workflow_id` set → 404/422 consistent with the router's existing error style).

**Tests** `tests/workflow_schedule_test.py`:
- Model level: ScheduleData round-trips the new fields.
- Router level (full app): create a workflow-target schedule (fake-credential model so runs succeed) → 201; creating with unknown workflow_id → 4xx.
- Manager level: instantiate `SchedulerManager` with a recording `workflow_starter` stub; call the private `_trigger` path (or fire via `notify_changed`+short cron if practical — prefer calling the internal trigger closure directly like existing scheduler tests do — read `tests/` for scheduler test patterns) → starter called once with (user_id, record); starter None → warning, no raise; starter raising → swallowed+logged.
- E2E (full app): workflow-target schedule + `await scheduler_manager.reconcile()`/fire → a run record exists with `trigger == "schedule"` and eventually SUCCEEDED (fake model).

- [ ] TDD → PASS → commit `feat(workflow): scheduled workflow runs`.

---

### Task 3: `run_workflow` chat tool

**Files:**
- Create: `src/agentscope/app/_tool/_workflow_run.py` — `WorkflowRun(ToolBase)` following `_manager/_scheduler/_tools/_schedule_create.py`'s structure (params pydantic model, `__init__` taking injected deps, `async def call(...)` returning ToolChunks):

Params: `workflow_name_or_id: str` (resolve: exact id first, else unique name match — list the user's workflows via storage; ambiguous name → error chunk listing candidates), `input: dict[str, Any] = {}` (pydantic dict param), `wait_seconds: int = Field(default=30, ge=0, le=300)`.

Behaviour: `start_run(trigger="chat_tool", chat_model_config=None — the RUN resolves models from agent templates; if the workflow has modelless nodes and no template models, start_run 422s → report as an error chunk with the problems)`; `engine.dispatch`; then poll `get_workflow_run` every 0.5s up to `wait_seconds`: terminal → success chunk summarising (`run SUCCEEDED/FAILED, node statuses one-line, output text of terminal agent nodes ≤500 chars`); timeout → chunk reporting run_id + "still RUNNING — check the runs page". 422/404 from start_run → friendly error chunk. All exceptions → error chunk, never raise.

`__init__(self, user_id, storage, workflow_service, workflow_engine)` — mirror how ScheduleCreate takes what it needs.

- Modify: `src/agentscope/app/_service/_toolkit.py` — `get_toolkit` gains `workflow_tools: list[ToolBase] | None = None` kwarg appended near the scheduler tools; `_service/_chat.py` (its caller — find the call site) builds `[WorkflowRun(user_id=..., storage=..., workflow_service=..., workflow_engine=...)]` — ChatService needs the engine: check what ChatService holds; thread `workflow_engine` through its constructor from `_lifespan.py` (mirroring how `sop_service` is attached post-construction — `chat_service.sop_service = sop_service` pattern — use the same late-attachment: `chat_service.workflow_engine = workflow_engine` with a None default, set in lifespan AFTER the engine is built; note the engine is built after chat_service in the current lifespan order — verify and adjust order if needed).
- Tool description (the LLM-facing text): concise English — "Start a workflow by name or id and optionally wait for it to finish. Returns the run's outcome summary or the run id if still running."

**Tests** `tests/workflow_run_tool_test.py`:
- Unit-ish (fake-credential full app or direct tool construction over fakeredis): happy path with `wait_seconds` enough → success chunk contains SUCCEEDED summary; slow run + `wait_seconds=0` → chunk contains run id + still-RUNNING wording; unknown name → error chunk listing nothing-found; ambiguous name → error listing candidates; start_run 422 (modelless workflow) → error chunk contains a problem line.
- Toolkit registration: `get_toolkit(...)` with the new kwarg includes the tool (read how existing toolkit tests assert — find any; else assert via the assembled Toolkit's tool list API).

- [ ] TDD → PASS → commit `feat(workflow): run_workflow chat tool`.

---

### Task 4: Hot-loop + listing performance (recorded notes)

**Files:**
- Modify: `src/agentscope/app/storage/_redis_storage.py`:
  1. `upsert_workflow_run` (and `upsert_workflow`? NO — scope to runs only, the hot path): wrap the record SET + index SADD/SREMs in ONE `async with self._client.pipeline(transaction=False) as pipe:` block (check fakeredis pipeline support — it supports it; keep `_set_with_ttl` semantics: replicate its SET (+optional EXPIRE) inside the pipeline — read it first).
  2. `list_workflow_runs` + `list_active_workflow_runs`: replace per-id GET loops with `MGET` (`await self._client.mget(keys)`) + per-record `model_validate_json`, preserving prune/sort semantics.
- Add run-list STATUS filter (recorded spec §6 drift): `list_workflow_runs(user_id, workflow_id=None, status: RunStatus | None = None)` — Redis filters post-fetch; SQL adds `WHERE status ==`. Update `_base.py` signature + both backends + router `?status=` query param + schema docs. Contract-mixin tests updated (add a status-filter case).

**Tests** `tests/workflow_run_storage_perf_test.py` — extend the existing contract mixin in `tests/workflow_run_storage_test.py` instead of a new file where natural: pipeline equivalence (round-trip + active-index lifecycle still pass unchanged — they ARE the regression tests), mget equivalence (existing list tests), new status-filter contract test both backends, SQL suite green, `tests/workflow_engine_test.py` green (the hot loop exercises upsert heavily).

- [ ] TDD → PASS → commit `perf(workflow): pipeline run upserts, mget listings, status filter`.

---

### Task 5: Regression sweep

- [ ] Full battery (all workflow suites + agent + sop + storage + e2e + hub + access) — green.
- [ ] Line lengths ≤79, annotations complete on touched files.
- [ ] Commit any fixups. Final review gate.

## Follow-up plans

- **Plan 4 — Frontend:** React Flow builder, run view (SSE from `workflow_run:{run_id}`), approvals inbox, agent template form fields, SchemaForm debt, SessionOrigin TS types, schedule-UI for workflow targets, `run_workflow` tool surfaced in chat UI, credential Test button.
