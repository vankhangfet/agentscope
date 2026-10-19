# Workflow Run UI & Loop Fixes — Design (v1)

- **Date:** 2026-10-19
- **Status:** Approved (run button + dialog everywhere useful; iterate body-agent fix; loop client validation; README accuracy)
- **Goal:** Make workflows runnable from the Web UI (the missing Run button) and remove the two form-level blockers found in review.

## Problem

1. `useStartRun` exists but is wired nowhere — no page can start a run; users must fall back to Swagger/Schedule/chat.
2. Loop nodes in `iterate` mode hide the *Body agent* field, but the engine requires `body_agent_id` when the list is non-empty — the node fails at dispatch with no way to configure it.
3. A fresh iterate loop (empty `list_template`) passes client validation and only fails as a server 422 on Save.

## Run dialog (new `RunWorkflowDialog`)

- Fields: **Input (JSON)** textarea (validated client-side, default `{}`, error inline on bad JSON), **Default model** (optional LlmSelect → `chat_model_config`; hint: required for retry-until loops' verifier), optional workflow **Select** when launched without a workflow context.
- Confirm → `useStartRun` (`POST /workflow/{id}/run`, `trigger: "manual"`) → navigate to `/workflow/runs/{run_id}`; mutation errors toast (house client).
- Entry points: **builder header "Run"** (enabled only when saved and problem-free — dirty state shows a "Save first" tooltip/disabled hint), **list card "…" menu → Run**, **Runs page empty state** ("Run a workflow" — dialog with workflow select).

## Loop fixes

- `NodeConfigPanel`: *Body agent* select rendered in **both** loop modes (iterate + retry_until); label per mode if needed.
- `validateGraph.ts`: new problem — iterate loop with empty `list_template` ("Loop 'x' needs a list template"); Save stays disabled until fixed (same Problems strip UX).

## README accuracy

- §Workflows: "Watch runs live (per-node status, logs, outputs)" → drop "logs" (no log stream exists — status/error/output only).
- §Scheduling line: `run_workflow` → `WorkflowRun` (actual tool name).

## Non-goals

Autosave / dirty-guard; per-node log streaming; schedule/chat passing run models; approval live timers (boot-sweep only, unchanged); parallel iterate.

## i18n

en/zh for every new string (dialog title/fields/hints, problems text, menu entry).
