# Agent Workflow Platform — Design Spec

- **Date:** 2026-10-08
- **Status:** Approved (design phase complete, pending implementation plan)
- **Scope:** Extend `agentscope/app` (agent_service) and `examples/web_ui` with agent templates, a drag-and-drop DAG workflow builder, and workflow execution.

## 1. Overview

Turn the existing `examples/agent_service` + `examples/web_ui` pair into an internal team platform where users can:

1. **Configure custom models** — add provider API keys and OpenAI-compatible endpoints (vLLM / LiteLLM / Ollama), then pick any model per agent.
2. **Build agents on the web UI** — save reusable agent templates (name, system prompt, model + credential, tools/MCP, memory, permissions).
3. **Build workflows on a drag-and-drop canvas** — connect agents into DAGs with condition, loop, and human-approval nodes; run them from the UI, on a schedule, via REST API, or as a chat tool.

## 2. Decisions

| Topic | Decision |
|---|---|
| Foundation | Extend `agent_service` in this repo — reuse credentials, sessions, SOP patterns, scheduler, permission system, storage |
| Workflow UX | Full drag-and-drop DAG from v1 (n8n/Dify style, React Flow) |
| Node types (v1) | Agent, Condition/Branch, Loop/Iteration, HumanApproval |
| Audience | Company-internal team — existing auth + access-policy sharing |
| Triggers | Manual UI run, schedule (existing scheduler), REST API/webhook, chat tool (`run_workflow`) |
| Custom model level | Provider credentials + OpenAI-compatible `base_url` (existing system) + a new test-connection endpoint. No uploaded provider code in v1. |
| Language | All UI text, code identifiers, and comments in English |

## 3. Architecture

Three new backend modules inside `src/agentscope/app/`, one new engine, and new frontend pages. Existing modules are consumed, not modified where avoidable.

```
workflow_router        → CRUD + run + approve + retry endpoints
agent_template_router  → CRUD for agent templates
workflow_engine        → DAG executor (frontier scheduler, node handlers,
                         blackboard context, persistence, park/resume)
```

Reuse map:

- **Agent node execution** → subagent session infrastructure (`_tool/_agent_create`, `_tool/_agent_invite` patterns)
- **Loop verifier semantics** → SOP step/verifier pattern (`agentscope/sop`)
- **Human approval** → permission system / HITL parking + verdict flow (as SOP runs do)
- **Scheduled runs** → `scheduler_manager`
- **Run events → UI** → existing message bus, streamed over SSE
- **Model access** → existing `ChatModelConfig` + credential resolution (`_service/_model.get_model`)

Interactive mockups from the brainstorming session live in `.superpowers/brainstorm/149269-1791454535/content/` (overview, architecture, engine-semantics, frontend).

## 4. Data model

Three new record types stored through `StorageBase` (Redis default; SQL backends follow the existing storage abstraction).

```python
class AgentTemplateRecord(Record):
    data: AgentTemplateData
    # owner_id via record envelope (same pattern as other records)

class AgentTemplateData(BaseModel):
    name: str
    description: str = ""
    system_prompt: str
    chat_model_config: ChatModelConfig          # reuse existing schema
    fallback_chat_model_config: ChatModelConfig | None = None
    tools: list[MCPToolRef] = []                # workspace MCP references
    memory: bool = False                        # AgenticMemoryMiddleware
    permission_context: PermissionContext | None = None

class WorkflowRecord(Record):
    data: WorkflowData

class WorkflowData(BaseModel):
    name: str
    description: str = ""
    graph: WorkflowGraph                        # React Flow-compatible JSON
    version: int                                # bumped on every PATCH

class WorkflowGraph(BaseModel):
    nodes: list[WorkflowNode]                   # id, type, position, config
    edges: list[WorkflowEdge]                   # id, source, target, branch label

class WorkflowRunRecord(Record):
    data: WorkflowRunData

class WorkflowRunData(BaseModel):
    workflow_id: str
    workflow_version: int
    status: Literal[
        "PENDING", "RUNNING", "WAITING_HUMAN",
        "SUCCEEDED", "FAILED", "CANCELLED",
    ]
    input: dict
    context: dict[str, Any]                     # blackboard: node_id → output
    node_states: dict[str, NodeRunState]        # node_id → status/attempts/output/error
    trigger: Literal["manual", "schedule", "api", "chat_tool"]
    created_at / updated_at / finished_at: datetime | None
```

The graph is stored in **React Flow schema** (nodes carry `position`; edges carry ids). The engine ignores presentation-only fields and reads `type` + `config`.

### Node configs

- **AgentNode:** `template_id`, `prompt_template` (may reference `{{nodes.<id>.<field>}}` / `{{input}}`), `max_retries`, optional output schema.
- **ConditionNode:** list of `branches: [{label, rule}]` — rule is either a JSON-path/expression on the blackboard or `llm_judge` (a call to the model configured on this node returning a branch label). Exactly one outgoing edge per branch label.
- **LoopNode:** mode `iterate` (list from blackboard, sequential or bounded-parallel) or `retry_until` (body rerun until a verifier prompt passes, `max_attempts`, reusing the SOP verdict pattern).
- **HumanApprovalNode:** `approvers` (user ids / role), `timeout_hours` (default 24), `timeout_branch` (`fail` | edge label).

## 5. Engine semantics

### Run lifecycle

`PENDING → RUNNING → (WAITING_HUMAN ⇄ RUNNING) → SUCCEEDED | FAILED | CANCELLED`

- Every node-state transition is persisted before dispatch continues.
- A run left `RUNNING` while the service was down is detected at startup (stale heartbeat) and resumed from persisted node states.

### Frontier scheduling

1. Compute ready nodes: all incoming edges sourced from `SUCCEEDED` nodes.
2. Dispatch ready nodes in parallel with a bounded worker pool (default 4).
3. If no node is ready and unfinished nodes remain → the run must be parked on `WAITING_HUMAN`; otherwise the graph is invalid (rejected at save time anyway).
4. Run finishes when all terminal nodes have terminal states.

### Blackboard context

- Each node writes `context[node_id] = { "text": ..., "structured": ... }`.
- Node inputs are mustache-style templates rendered from the blackboard plus the run `input`.
- Unresolvable references fail the node with a clear template error (surfaced in node state).

### Node handler behavior

| Node | Runs | Output |
|---|---|---|
| Agent | spins subagent from template, streams logs, runs to completion | `{ text, structured }` |
| Condition | evaluates rules or LLM judge → selects one outgoing edge | `{ branch }` |
| Loop | iterates list / retries until verifier passes | `{ items, attempts }` |
| HumanApproval | parks run, notifies approvers, resumes on verdict | `{ verdict, note }` |

### Retry policy

`max_retries` per node (default 2). Retryable exceptions follow the model classes' `_get_retryable_exceptions()`. Exhausted retries ⇒ run `FAILED`; `POST /retry` re-runs from the failed node with the blackboard intact.

## 6. API surface

```
# Workflows
POST   /workflow                      create (validates acyclic + configs + credentials)
GET    /workflow                      list (own + shared)
GET    /workflow/{id}                 detail (graph + versions)
PATCH  /workflow/{id}                 update → version += 1
DELETE /workflow/{id}

# Runs
POST   /workflow/{id}/run             body {input} → new run (manual trigger)
GET    /workflow/runs                 list (filter workflow/status)
GET    /workflow/runs/{run_id}        node_states + context
POST   /workflow/runs/{run_id}/approve   {node_id, verdict: approve|reject, note}
POST   /workflow/runs/{run_id}/cancel
POST   /workflow/runs/{run_id}/retry     from failed node

# Agent templates
POST/GET/PATCH/DELETE /agent-template

# Small addition to credentials
POST   /credential/test               one cheap request to verify key/base_url
```

## 7. Triggers

- **Manual:** `POST /workflow/{id}/run` from the builder/run page.
- **Schedule:** entries in the existing scheduler create runs (`trigger: "schedule"`).
- **API/webhook:** the same run endpoint with API-key auth via existing access control.
- **Chat tool:** `run_workflow` registered as a builtin tool; a chat agent calls it, gets a summary of the run result. Approval prompts can surface in the chat session.

## 8. Frontend (`examples/web_ui`)

New pages (English UI):

1. **Workflows** — list, versions, duplicate, delete.
2. **Workflow Builder** — React Flow canvas; left node palette; right config panel (reuses the existing credential/model picker); client-side cycle detection on edge connect; save validates via API.
3. **Runs** — list with status chips; detail view = read-only canvas with per-node status colors, live log panel for the selected node (SSE), cancel/retry actions.
4. **Agent Templates** — form + list, model/credential pickers, tool toggles.
5. **Approvals** — inbox of `WAITING_HUMAN` items with context preview, approve/reject + note.
6. **Credentials** — existing page plus a **Test connection** button.

## 9. Error handling

- **Graph validation on save:** acyclicity, required node configs, branch-label/edge matching, referenced credentials exist. Errors cite the offending node/edge ids.
- **Node failure:** retries → `FAILED` with error stored in `node_states`; run can be retried from the failed node.
- **Crash recovery:** persisted states + stale-run sweep at startup.
- **Approval timeout:** `timeout_hours` (default 24) → follow `timeout_branch` (`fail` or a labeled edge).
- **Frontend:** SSE reconnect with state refetch; optimistic status chips.

## 10. Testing

- **Unit:** graph planner (ready set, cycle detection), node handlers against a fake chat model, template rendering, storage round-trip (Redis test instance).
- **Integration:** end-to-end runs with a fake model — happy path, branch selection, loop retry-until-verifier, human park → approve → resume, crash → restart → resume.
- **Frontend:** builder smoke test (add node, connect, save) and run-view status rendering; manual QA checklist for canvas interactions.

## 11. Non-goals (v1)

- Uploaded custom provider code (level-3 models)
- Billing, quotas, rate limiting per tenant
- Public-SaaS hardening
- Sub-workflow nodes and typed ports (v2 candidates)
- Editing SOPs visually (SOP feature stays as-is)
