# Workflow Notify Node & Template Catalog — Design (v1)

- **Date:** 2026-10-19
- **Status:** Approved (full: notify node on 4 channel types + 4-template catalog + gallery/mapping UI + README)
- **Goal:** Workflows can send notifications to configured channels (a first-class node), and users can start from shipped templates instead of a blank canvas.

## Verified facts this design builds on

- Node architecture: discriminated union `WorkflowNodeConfig` in `storage/_model/_workflow.py` (4 kinds); planner is kind-agnostic for non-branching nodes (no change); executor `NodeHandlers._handlers` dict in `_service/_workflow_nodes.py`; `_render`/`_blackboard` helpers; `_retry_left` in the engine is kind-branched; `_validate` in `_service/_workflow.py` checks per-kind references at save; `ChannelClients` built in `_lifespan.py` and stored on `app.state`.
- Channels: NO unified send-text exists — per-type methods (MSTeams `send_notification(title, text)`; Feishu/DingTalk `send_message_to(target, text)`; Telegram `_send_message` private; Discord needs a live channel object). `ChannelClients.get(channel_id)` builds/caches instances, returns None if record missing/disabled.
- Rendering: `render_template(template, context)` (mustache-ish `{{dotted.path}}`) already used by node templates.
- No built-in HTTP tool for agents, but the workspace shell tool (PowerShell/Bash) can `curl`/`Invoke-RestMethod` — templates instruct that.
- Catalog house pattern: static module + `GET` endpoint with no auth deps (MCP presets, factory roster). Workflow create `POST /workflow/` accepts a full graph (Duplicate already reuses it).

## A. `Notify` node kind (5th)

- **`NotifyNodeConfig`** (`type: Literal["notify"]`): `channel_id: str`, `message_template: str` (non-empty), `max_retries: int = 2` (0-10, like agent nodes). Non-branching: single source handle, planner untouched.
- **Unified `ChannelBase.send_text(text: str) -> None`**: default raise `NotImplementedError`; implemented by `MSTeamsChannel` (`send_notification(None, text)`), `TelegramChannel` (public wrapper over `_send_message` using its configured default chat — resolve from `routing`/credentials exactly as inbound replies do), `FeishuChannel` (`send_message_to` with the receive id from its routing/platform_config default target), `DingTalkChannel` (same pattern). `DiscordChannel` NOT implemented in v1 (needs a live channel object) — the node fails with a clear "channel type X does not support workflow notifications" error; documented.
- **Executor handler `"notify"`**: resolve channel via the injected `ChannelClients.get(channel_id)` (None → NodeFailure "channel missing or disabled"); render `message_template` against `_blackboard(run)` (RenderError → NodeFailure with the path); `await channel.send_text(text)`; close if the client needs it (follow `TeamsNotify` tool discipline); output `{"channel": <name-or-type>, "sent": True}`.
- **Wiring**: `NodeHandlers.__init__` gains `channel_clients`; `WorkflowEngine.__init__` passes it through; `_lifespan.py` passes `channel_clients`. `_retry_left`: notify uses `max_retries` like agent nodes.
- **Save validation** (`_service/_workflow.py::_validate`): notify node's channel record exists (and is enabled) for the user → problem "Node 'x' references unknown/disabled channel 'y'".
- **Run-time** (`_resolve_node_models`): notify contributes nothing (no model).

## B. Template catalog (backend static)

- **`_service/_workflow_templates.py`**: frozen list of `WorkflowTemplate{id, name, description, category, graph: WorkflowGraph, agent_slots, channel_slots}` where slots derive from the graph (agent node ids + one-line role descriptions; notify node ids). `template_view()` projection like presets. Graph validation: import-time sanity (each template's graph passes `validate_workflow_graph` with `agent_id=""` allowed only for slots — see C).
- **Endpoint** `GET /workflow/templates` (no auth deps, registered before `/{workflow_id}` routes; MCP-presets precedent).
- **Templates** (agent nodes carry `agent_id: ""` placeholders; notify nodes carry `channel_id: ""`):
  1. `channel-notification` (Category: Channels) — input → notify (`{{input.message}}`).
  2. `product-design` (Category: Design) — brief agent → propose agent → human approval → refine agent → notify (summary template).
  3. `news-digest` (Category: Digest) — digest agent (topic from input; collect + summarize via shell/MCP tools) → approval → notify.
  4. `vietcombank-fx` (Category: Digest) — fetch agent (PowerShell/Bash curl to Vietcombank public rates endpoint, format a table) → notify (rates text to channel; pairs with a daily Schedule).

## C. Model leniency + install flow

- `validate_workflow_graph`/pydantic: `AgentNodeConfig.agent_id` stays `str` — templates use `""` and the SERVICE `_validate` gains "Node 'x' needs an agent" (client mirrors) so a slot-empty graph can be SAVED as a draft? **No** — keep saves strict: `""` agent/channel = save-blocked problem (client + server). The mapping happens in the install dialog BEFORE create; the builder's strictness then acts as the safety net for hand edits.
- **Install** (frontend): gallery dialog (template cards w/ category + description) → mapping dialog (agent slots → user's agents via AgentSelect; channel slots → enabled channels via useChannels; Create disabled until all slots mapped) → `createWorkflow({name: template.name, graph: mapped})` → navigate builder.
- **Entry points**: New button split (Blank / From template); empty state gains "Use a template".

## D. Frontend node support

`NodeKind` + `NODE_META` (icon `Send`, accent) + palette entry + `NotifyForm` (channel Select from `useChannels` enabled-only + message template textarea + max retries) + `defaultConfig('notify')` + `validateGraph` (needs channel / needs message template) + `PLAIN_KEYS.notify = ["channel","sent"]` + notify node card component + i18n (palette name/description, form labels/hints, problems).

## Non-goals (v1)

Discord notify; per-branch notify; message templating beyond blackboard paths; template versioning/params; editing templates in UI; schedule creation inside install (user links via Schedule page after).

## README

§Workflows: add "send notifications to your configured channels (notify nodes)" and "start from built-in templates (channel notification, product design, news digest, Vietcombank FX rates)".
