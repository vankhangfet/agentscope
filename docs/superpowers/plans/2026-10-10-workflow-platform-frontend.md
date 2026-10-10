# Workflow Platform — Frontend Implementation Plan (Plan 4 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the web UI: workflow builder (React Flow canvas), runs view with live node statuses (SSE), approvals inbox, agent-template form fields, credential test button — completing the platform's spec section 8.

**Architecture:** Follows the web_ui's established per-resource pattern: `src/api/<resource>.ts` typed client + `src/hooks/use<Resource>.ts` (react-query) + `src/pages/<resource>/` + routes in `App.tsx` + i18n keys. One small backend addition: the run-event SSE endpoint (the bus channel `workflow_run:{run_id}` exists; the UI needs an HTTP stream). No frontend unit-test runner exists — the per-task gate is `pnpm build` (tsc strict + vite) and `pnpm lint` plus the Python battery for backend tasks.

**Tech Stack:** React 19, TypeScript, react-router-dom v7, @tanstack/react-query, @xyflow/react (React Flow 12 — NEW dependency), Tailwind/shadcn components already present, i18next (en keys required; zh may fall back).

**Spec:** `docs/superpowers/specs/2026-10-08-workflow-platform-design.md` section 8 + mockups in `.superpowers/brainstorm/149269-1791454535/content/` (overview.html, frontend.html).
**Recorded debts folded in:** SchemaForm array/object widgets; `SessionOrigin` TS types (`'sop'`/`'workflow'`); schedule UI workflow-target fields; runs-page phrasing.

**UI text in ENGLISH** (project constraint). **Branch:** `workflow-frontend` (from `main`).

**Verification commands** (from `examples/web_ui/frontend/`): `pnpm build`, `pnpm lint`. Backend: `.venv/Scripts/python.exe -m pytest <file> -v` from repo root. Install deps: `pnpm add @xyflow/react` (workspace root `examples/web_ui`).

---

## File Structure

| Area | Files |
|---|---|
| Backend SSE | Modify `src/agentscope/app/_router/_workflow.py` (+ stream endpoint); Create `tests/workflow_run_stream_test.py` |
| API client | Create `src/api/workflow.ts`; Modify `src/api/index.ts`, `src/api/types.ts` (SessionOrigin fix) |
| Hooks | Create `src/hooks/useWorkflows.ts`, `src/hooks/useWorkflowRuns.ts` |
| Pages | Create `src/pages/workflow/` (index.tsx list, builder.tsx, runDetail.tsx, approvals.tsx + components/); Modify `src/App.tsx` (routes), nav component, `src/i18n/locales/en.json` |
| Agent form | Modify `src/components/dialog/AgentDialog.tsx`, `EditAgentDialog.tsx`, `src/components/form/` (array/object widget) |
| Credentials | Modify credential page/components (Test button) |
| Schedule | Modify schedule dialog for workflow-target fields |

---

### Task 1: Run-event SSE endpoint (backend)

**Files:** Modify `src/agentscope/app/_router/_workflow.py`; Test `tests/workflow_run_stream_test.py`.

**Endpoint:** `GET /workflow/runs/{run_id}/stream` — SSE (`text/event-stream`) mirroring the session-stream endpoint's implementation (READ `src/agentscope/app/_router/_session.py`'s stream route + how it subscribes the bus and formats events; reuse its response/streaming helpers and heartbeat pattern). Behavior:
1. 404 unknown run (owner-scoped).
2. On connect: emit `event: state` with the FULL current run record JSON (so the UI paints immediately).
3. Subscribe `workflow_run:{run_id}` on the bus; each payload → `event: node`/`event: run` SSE lines (the payload already carries `{type, run_id, node_id, status, error}`).
4. Heartbeat/keepalive comment lines per the session stream's cadence; clean unsubscribe on disconnect (mirror session stream teardown).

**Tests** (fakeredis app + fake credential, mirroring `tests/app_workflow_run_router_test.py`): stream endpoint returns 200 text/event-stream; initial `state` event carries the run; starting a run (POST run → poll WAITING_HUMAN via the stream reader) surfaces `node` events; 404 on unknown; disconnect unsubscribes (bus subscription count returns to baseline — check how session-stream tests assert this; mirror).

- [ ] TDD → PASS + `tests/app_workflow_run_router_test.py` green → commit `feat(workflow): run event SSE stream`.

### Task 2: API client + types + hooks

**Files:** Create `src/api/workflow.ts`, `src/hooks/useWorkflows.ts`, `src/hooks/useWorkflowRuns.ts`; Modify `src/api/index.ts` (export), `src/api/types.ts`.

**`api/workflow.ts`** — typed client mirroring `src/api/schedule.ts`'s style (read it): 
- Types: `Workflow`, `WorkflowGraph {nodes: WorkflowNode[]; edges: WorkflowEdge[]}`, `WorkflowNode {id; position {x,y}; config: AgentNodeConfig | ConditionNodeConfig | LoopNodeConfig | HumanApprovalNodeConfig}` (discriminated unions matching the backend pydantic shapes — mirror field names EXACTLY from the OpenAPI/Python models), `WorkflowRun`, `NodeRunState`, `RunStatus`.
- Functions: `listWorkflows`, `getWorkflow`, `createWorkflow`, `updateWorkflow`, `deleteWorkflow`, `startRun`, `listRuns({workflowId?, status?})`, `getRun`, `approveRun(runId, {node_id, verdict, note})`, `cancelRun`, `retryRun`, `testCredential(credentialId, {model, parameters})`.
- `openRunStream(runId, onState, onEvent, signal?)` — fetch-based SSE reader mirroring `src/api/session.ts`'s stream helper (read it; same parsing/heartbeat/disconnect handling).

**Hooks** (react-query, mirror `useSchedules.ts`): `useWorkflows()` list + invalidation helpers; `useWorkflowRuns(filters)`; mutation wrappers (create/update/delete/start/approve/cancel/retry) each invalidating the right query keys.

**`types.ts`:** extend the `SessionOrigin` union with `'sop' | 'workflow'` (recorded debt).

Gate: `pnpm build` + `pnpm lint` clean. Commit `feat(web-ui): workflow api client and hooks`.

### Task 3: Workflows list page + routing

**Files:** Create `src/pages/workflow/index.tsx` (+ small components); Modify `src/App.tsx` (routes `/workflow`, `/workflow/builder/:workflowId?`, `/workflow/runs/:runId`, `/workflow/approvals`), the nav/sidebar component (find where Schedule/Channel links render — likely `src/components/layout/`), `src/i18n/locales/en.json`.

List page mirrors the Schedule page's layout patterns (read `src/pages/schedule/index.tsx`): table/cards of workflows (name, description, node count, updated_at, version), actions: New → builder (`/workflow/builder/`), Edit → builder with id, Duplicate (fetch → create copy "name (copy)"), Delete (confirm dialog), "Latest runs" quick link. Empty state with a "Create your first workflow" call-to-action. i18n keys under `workflow.*`.

Gate: build + lint. Commit `feat(web-ui): workflows list page`.

### Task 4: Builder canvas (React Flow)

**Files:** `pnpm add @xyflow/react`; Create `src/pages/workflow/builder.tsx` + `src/pages/workflow/components/` (`NodePalette.tsx`, `WorkflowCanvas.tsx`, `NodeConfigPanel.tsx`, `nodeTypes.tsx`, `validateGraph.ts`); i18n keys.

**Behavior:**
- Route `/workflow/builder/:workflowId?` — no id = new workflow (name prompt in a header input).
- Canvas with 4 custom node types (`nodeTypes.tsx`) — colored headers + icons per type (agent=Bot, condition=GitBranch, loop=Repeat, approval=UserCheck from lucide), showing name + a config summary line; approval/condition nodes render their branch handles as labeled edge source-handles.
- Left palette: drag-to-canvas (React Flow onDrop pattern) or click-to-add.
- Right config panel on node select — per type form: agent (agent picker via `useAgents`, prompt_template textarea, max_retries number); condition (branches list: label+rule rows, add/remove, optional model select); loop (mode toggle, list_template / body agent + item_template + max_attempts per mode); approval (approvers array input, timeout_hours, timeout_branch).
- Edges: connect nodes; edge branch-label dropdown when source is condition/approval (labels from the node's config).
- `validateGraph.ts` — CLIENT-side port of `validate_workflow_graph` rules (cycle DFS, duplicate ids, branch-label matching, approval approve/reject edges); problems list shown in a dismissible panel; Save disabled while invalid. (Mirror the Python semantics from `src/agentscope/app/storage/_model/_workflow.py` — read it.)
- Save: create or update via API; toast; navigate stays (URL gains the id on first save).
- Positions persist in the graph (backend nodes carry `position`).

Gate: build + lint; manual smoke via dev server noted in report (start `pnpm dev` briefly if practical — otherwise state not run and why). Commit `feat(web-ui): workflow builder canvas`.

### Task 5: Runs list + run detail (live view)

**Files:** Create `src/pages/workflow/runs.tsx` (list; or fold into index page's tab), `src/pages/workflow/runDetail.tsx` + components (`RunGraphView.tsx` — read-only mini canvas reusing nodeTypes, node status colors/badges; `NodeOutputPanel.tsx`); i18n.

**Behavior:**
- Runs list: table (workflow name, status chip, trigger, started, finished), filters workflow/status (uses Task 2's `listRuns` params), poll 5s OR navigate to detail.
- Detail (`/workflow/runs/:runId`): header (status chip, workflow name/version, trigger, error line, Cancel button when active, Retry when FAILED, Approve prompt when the run is WAITING_HUMAN — verdict buttons + note input, calling `approveRun`); body: `RunGraphView` (read-only React Flow, node border/background tinted by `node_states[id].status`, animated edge for RUNNING frontier — derive ready edges from statuses like the planner does) + `NodeOutputPanel` for the selected node (output JSON/text, attempts, error); live updates via `openRunStream` (state event paints, node events patch in place; reconnect-on-drop with backoff like the chat stream).
- Polling fallback when SSE fails (status query refetch).

Gate: build + lint. Commit `feat(web-ui): runs list and live run detail`.

### Task 6: Approvals inbox

**Files:** Create `src/pages/workflow/approvals.tsx` (+ components); nav badge; i18n.

Behavior: lists runs with `status=WAITING_HUMAN` (via `listRuns({status:'WAITING_HUMAN'})`), each card showing workflow/node (find the WAITING_HUMAN node), started time, context summary (last agent output preview), Approve/Reject buttons + note field → `approveRun`; badge count in the sidebar nav (lives off the same query, refetch 15s). Commit `feat(web-ui): approvals inbox`.

### Task 7: Agent template fields + SchemaForm debt

**Files:** Modify `src/components/dialog/AgentDialog.tsx`, `EditAgentDialog.tsx`, `src/components/form/SchemaForm.tsx` (+ a new `ArrayField`/`ObjectField` widget component); i18n.

Behavior: the agent form renders `chat_config` section with: model select (REUSE the existing `LlmSelect` component + `useCredentials`/`useAvailableModels` pattern from chat's model picker — read how the session model picker builds its options) storing `{type, credential_id, model, parameters}`; tools = simple string-array input (add/remove chips); memory = checkbox. Submit maps into `chat_config` (create) / PATCH (update). SchemaForm debt: array-of-string props render the new `ArrayField` instead of the text-input fallback (guard: only `type: "array"` with string items); note in code where object fields still fall back. Existing agent CRUD tests/flows untouched. Commit `feat(web-ui): agent template model tools memory fields`.

### Task 8: Credential Test button + schedule workflow-target UI

**Files:** Modify credential page components (Test dialog: model input + parameters JSON → `testCredential` → ok/fail banner with detail); Modify the schedule create/edit dialog (find `src/components/dialog/Schedule*` or the schedule page's form): target toggle "Agent / Workflow" — workflow mode shows workflow picker (`useWorkflows`) + run_input JSON editor, hides agent/model fields; create/update payloads include `workflow_id`/`run_input`. i18n. Commit `feat(web-ui): credential test and workflow schedules`.

### Task 9: Sweep + final review

- [ ] `pnpm build` + `pnpm lint` clean; Python battery (Plan 3's full list) green.
- [ ] Dev-server smoke of the five pages noted in the final report (what was manually verified via `pnpm dev` + browser or explicitly not run).
- [ ] Final whole-branch review gate.

## Notes for implementers

- READ a sibling page/hook/api module before writing each file — house patterns win over generic React style.
- i18n: all user-facing strings via `t('workflow.*')`; en.json keys complete (zh may fall back — acceptable per constraint).
- The backend OpenAPI (`/docs` JSON via the running app or the router code) is the source of truth for shapes — the pydantic models in `src/agentscope/app/storage/_model/_workflow*.py` are the reference.
- Do NOT modify backend behavior beyond Task 1's endpoint.
