# Telegram + MS Teams Channels — Design & Plan

- **Date:** 2026-10-12
- **Status:** Approved (Approach A for Teams push)
- **Scope:** Two new channels — Telegram (full 2-way bot) and MS Teams (push notifications via Incoming Webhook + an agent tool to send notifications).

## Design

### Telegram — full 2-way bot

`src/agentscope/app/channel/_telegram/_channel.py` — `TelegramChannel(ChannelBase)`:

- **Credentials** (schema-driven form): `bot_token: str` (from @BotFather).
- **Config**: `allowed_chat_ids: list[str] = []` (empty = accept every chat; else only listed ids), `show_thinking/show_tool_process: bool = False` (mirror Feishu/Discord flags).
- **`start_listening(emit)`**: httpx long-polling loop on `https://api.telegram.org/bot<token>/getUpdates` (`timeout=25`, `offset` tracking from the last `update_id + 1`); each `message` with text → `ChannelEvent(channel_id, channel_user_id=from.id, channel_user_name=from.first_name, chat_id=str(chat.id), content=[TextBlock(text)])`; non-text messages ignored (v1); `get_updates` errors → log + sleep 3s + continue (self-healing reconnect per the base's contract); cancel → clean exit.
- **`send_response(event, events)`**: accumulate per the Discord template (`_EVENT_ADAPTER` loop, `RequireUserConfirmEvent` → v1 sends a plain-text confirmation request card — Telegram has no buttons without inline keyboards; v1 uses text), render via `self._render`, then `sendMessage` per text block, split into ≤4096-char chunks; `parse_mode` omitted v1 (plain text — Telegram Markdown escaping is error-prone; backlog).
- Pure httpx — no SDK dependency.

### MS Teams — push-only channel + notification tool

`src/agentscope/app/channel/_msteams/_channel.py` — `MSTeamsChannel(ChannelBase)`:

- **Credentials**: `webhook_url: str` (Teams channel → Connectors → Incoming Webhook).
- **Config**: none v1.
- **`start_listening`**: idle — `await asyncio.Event().wait()` (no inbound source; releases on cancel).
- **`send_response(event, events)`**: accumulate + render as Telegram; POST one MessageCard (`{"@type": "MessageCard", "@context": ..., "themeColor", "title"?, "text"}`) per text block to the webhook URL (httpx, 10s timeout, non-2xx → log error). `chat_id` is `"default"` (one webhook per channel instance) — inbound never creates one, the tool uses a synthetic event.
- **`send_notification(title, text)`** public method: the tool's backend — builds the card and POSTs; raises on HTTP failure (the tool renders the error).

**Tool** `src/agentscope/app/_tool/_teams_notify.py` — `TeamsNotify(ToolBase)`:
- Params: `title: str`, `text: str`.
- Behaviour: find the (single) MS Teams channel instance via `storage.list_channels(user_id)` filtering `channel_type == "msteams"`; none → friendly error chunk ("Create an MS Teams channel first"); >1 → use the first + note; call `send_notification`; exceptions → error chunk, never raise. Registered through `get_toolkit(..., channel_tools=...)` — check how channel tools flow today (the wiki tools ride `channel_tools`; the Teams tool is service-level like ScheduleCreate — wire it beside the scheduler tools in `_toolkit.py`, constructed with `storage` only, when any msteams channel exists… simpler: always construct; it errors politely when no channel exists).

### Registration

- `create_app(channels=[...])` gains both classes in `examples/agent_service/main.py` (schema-driven UI forms appear automatically; credential binding flow stays the generic create form — check `_registry` requires nothing more).
- Channel lifecycle dispatcher hosts them like any other (idle Teams listener is a no-op cost).

### Non-goals (v1)

- Telegram inline keyboards / callback buttons (approval cards as text).
- Telegram Markdown `parse_mode` escaping.
- Teams 2-way (Azure Bot Framework) — Approach B backlog: bind arbitrary sessions to a Teams channel so lifecycle dispatcher streams replies automatically.
- Teams Adaptive Cards (MessageCard is the universally-rendered format).

## Plan (4 tasks, branch `telegram-msteams-channels`)

1. **TelegramChannel** — TDD: unit tests over a fake httpx transport (getUpdates pagination/offset, allowed filter, chunked sendMessage, error-retry loop, cancel) + `send_response` accumulation (reuse the suite patterns from `tests/channel_*_test.py` — read them first). Gate: new suite + channel regression suites.
2. **MSTeamsChannel** — TDD: idle listener cancel; MessageCard POST via fake transport (card shape, error logging, non-2xx). Gate: suite.
3. **TeamsNotify tool** + toolkit wiring — TDD: no-channel error, happy path POST, multiple channels note. Gate: suite + `service_toolkit_test`.
4. **Register + sweep** — example `main.py` channels; registry tests; full channel + workflow battery.

Gates per task: Python suites only (no frontend changes needed — forms are schema-driven; nav needs nothing). ≤79 cols, English.
