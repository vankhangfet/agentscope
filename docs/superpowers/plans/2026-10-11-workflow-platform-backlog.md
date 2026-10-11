# Workflow Platform — Backlog Burn-down Plan (Plan 5, closing)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear every recorded robustness/polish backlog item from Plans 1-4's reviews, and disposition the observed flaky battery failure.

**Architecture:** Three small batched tasks (engine robustness / storage-admin / frontend polish) + a disposition task. Nothing here changes API contracts.

**Sources:** the backlogs recorded in `docs/superpowers/plans/2026-10-09-workflow-platform-engine.md` (Engine fast-follow), `2026-10-10-workflow-platform-triggers.md` (Task 2 review), `2026-10-10-workflow-platform-frontend.md` (review minors).

**All code in English. Branch:** `workflow-backlog` (from `main`). Gates: Python battery; frontend `pnpm build && pnpm lint`.

---

### Task 1: Engine robustness batch

**Files:** `src/agentscope/app/_service/_workflow_engine.py`, `_lifespan.py`; tests in `tests/workflow_engine_test.py` (+ lifespan touch).

1. **Loop-crash heartbeat** — `_forget_task` currently logs-and-pops when a loop task dies with an exception, leaving the run non-terminal with no dispatcher until restart. Add: on abnormal task completion (exception, not clean return/cancel), schedule a bounded re-dispatch — `asyncio.create_task(self._retry_dispatch(user_id, run_id, attempt))` with the run_id captured in the closure; `_retry_dispatch` re-dispatches unless the run is terminal or a live task exists; attempts capped (e.g. 3) with a warning log each time; give-up marks the run FAILED with `"engine loop crashed repeatedly"` + finished_at (recoverable via the existing retry endpoint). Persist user_id/run_id on the task (e.g. a small wrapper coroutine capturing them, or a dict keyed by task).
2. **`close()`** — cancel all live `self._tasks` (await-suppress), used in `_lifespan.py` teardown so storage/bus never close underneath loops (mirror how `chat_service`/`sop_service` hook shutdown — read the lifespan stack; enter a small async-context wrapper or call close() after `yield` in the shutdown section).
3. **Shared fail-the-run helper** — extract the "node X failed: ..." run-failure write used by `_dispatch_node`'s exhaustion path, `recover_stale_runs`' budget-exhaustion path, and (now) the heartbeat give-up; use it everywhere (also fixes the Plan-2 final-review note (i): recovery-path FAILED runs leave unreachable PENDING nodes PENDING — route the recovery path through the same finalize/unreachable-cancel logic as the normal path).
4. **404 noun drift** — engine's "Workflow run {id!r} not found." vs service's "Run {id!r} not found." — align both to "Run {id!r} not found." (check which tests pin each).
5. **Doc note** — module docstring of the engine: retries apply to ALL NodeFailures per type budget (spec §5's retryable-exception classes are moot because ChatService folds model errors into error-bearing replies) — one paragraph.

Tests: loop killed via injected failing runner → re-dispatched (attempts observable) → succeeds on the scripted 2nd try; persistent crash → run FAILED with the crash error after the cap; `close()` cancels a mid-flight loop without run corruption (run stays RUNNING, recovery sweep picks it up — assert via recover_stale_runs); recovery-path FAILED run now has unreachable nodes CANCELLED.

- [ ] Battery green → commit `fix(workflow): engine loop heartbeat, shutdown close, shared fail path`.

### Task 2: Storage & admin batch

1. **SQL list poison tolerance** — `_sql/_storage.py` `list_active_workflow_runs`: per-row `try/except ValueError → logger.warning + skip` around `_to_record` (mirror the Redis path's self-healing; ~6 lines + a poison test in `tests/storage_sql_test.py` mirroring the Redis one).
2. **Judge-agent dedupe** — `make_judge_agent_factory`: before upserting, look up an existing engine-owned judge agent by name (`list_agents(user_id)` + name match `workflow-branch-judge`/`workflow-verifier`... — read what names Task 8's factory actually uses; if names embed prompts, switch to a fixed marker name scheme like `workflow-judge` (system prompts are generic already)) and reuse its id; one row per user total. Update the factory's docstring; test with fakeredis (two factory calls + two engine boots → same agent id).
3. **Agent-delete cascade exclusion** — the agent-delete cascades (`_service/_session.py` ~:543 delete_agent path + both storages' schedule cleanup): skip schedule records whose `data.workflow_id` is set when cascading by agent_id (they only carry a placeholder). Find the exact deletion sites (the Plan-3 review cites `_service/_session.py:543`, `_redis_storage.py:875`, `_sql/_storage.py:545`) and add the filter where schedules are selected for deletion. Test: agent with a placeholder-targeting workflow schedule → delete agent → schedule survives.
4. **Scheduler-start-after-setter** — `_lifespan.py`: move `await stack.enter_async_context(scheduler_manager)` to AFTER `set_workflow_starter(...)` (LIFO teardown stops the scheduler earlier on shutdown — the safe direction; verify the comment). Boot test (`app_lifespan_dedicated_test.py`) stays green.

- [ ] Battery green → commit `fix(workflow): storage poison tolerance, judge dedupe, cascade guard, startup order`.

### Task 3: Frontend polish batch

**Files:** `examples/web_ui/frontend/src/` — `pages/workflow/` + `AppSidebar.tsx` + `components/select/LlmSelect.tsx`; i18n.

1. **`parkedNodes(run)` helper** — new `pages/workflow/lib.ts` (or beside runReadiness.ts); use in approvals cards, sidebar chip count, runDetail approve block (one predicate, one place).
2. **`RUN_TERMINAL_STATUSES`** — export from the same module; replace the two local copies (`runs.tsx`, `runDetail.tsx`).
3. **Sidebar chip freshness + hit target** — give the sidebar's runs query `refetchInterval: 60_000` (updates the chip for scheduled/other-tab arrivals even with the page unmounted); enlarge the chip hit area (min 24×24: padding/min-w/min-h on the Link) and label "Approvals: N waiting" (aria + tooltip).
4. **Post-create re-seed race** — builder: after a successful CREATE, set `seededFor` to the NEW id at the same moment as navigate (so the arriving detail refetch doesn't re-seed/clobber).
5. **LlmSelect inert add-item** — hide the "Add credential" menu item when `onAddCredential` is absent (fixes the agent-form + NodeConfigPanel dead item).
6. **Key rename** — `workflow.runs.goRun` → `workflow.runs.goWorkflows` (en+zh; the only usage updated).
7. Leave as recorded (do NOT do): object SchemaForm widget; control-swap race; card icon dimming; runDetail attempts-staleness comment; validation-message ordering.

- [ ] `pnpm build && pnpm lint` baseline → commit `fix(web-ui): shared run helpers, chip freshness, builder seed guard`.

### Task 4: Flake disposition + final sweep

1. Fold in the flake-hunt result (running separately): if a failing test is identified → fix or pin its timing margin; if 6 clean runs → record "not reproduced in N consecutive full batteries" in the plan's closing note and move on.
2. Full Python battery + frontend gates.
3. Final whole-branch review → merge.

## Closing note (fill at Task 4)

- Flake hunt outcome: **IDENTIFIED AND FIXED** — `RunStreamE2ETest::test_live_run_streams_events_until_terminal` asserted the state frame read WAITING_HUMAN, but subscription-live ≠ snapshot-taken: an approve racing the snapshot yields a truthful RUNNING frame. Production behavior correct (storage is truth, events converge); assertion now accepts both orderings with rationale (commit `39cb3c13`; 5× suite re-runs green). Hunt evidence: 1 failure in 10+ full-battery runs, caught on hunt-run 1 of 6.
- Task 1 review leftovers (accepted, at-least-once semantics): duplicate terminal publishes on the exhaustion path; `retry()` lacks a `_closed` check (self-limiting); `_recover_one` upserts outside the write lock (boot-time sweep precedes traffic).
