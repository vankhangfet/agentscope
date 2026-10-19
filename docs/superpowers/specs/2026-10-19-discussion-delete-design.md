# Discussion Delete — Design (v1)

- **Date:** 2026-10-19
- **Status:** Approved (full: cancel-then-delete + participant cascade + UI + 404-poll fix)
- **Goal:** Let users delete discussions — the record, and the participant agents/sessions a run created — from the Web UI.

## Existing (verified)

Storage `delete_discussion` exists on both backends (Redis cleans the per-user + global active indexes; SQL plain DELETE; owner-scoped; idempotent False; tested). Engine `cancel()` awaits task settlement. `delete_agent` cascades sessions (+ their message logs) and schedule/team refs. Participant agents are named `discussion:{id}:{key}` (key = debater name or `judge`). No DELETE route, no engine delete, no UI affordance exist.

## Backend

- **`DebateEngine.delete(user_id, discussion_id) -> None`** (raises 404-shaped errors like cancel):
  1. Get record (owner-scoped) → missing ⇒ 404.
  2. Non-terminal (PENDING/RUNNING) ⇒ `cancel()` first (awaits task settlement — no writer remains), tolerate its 404 race only if the record vanished (then proceed to step 3 semantics).
  3. Cascade participants: `list_agents(user_id)` filtered by name prefix `discussion:{id}:` → `delete_agent` each (existing cascade removes their sessions + message logs). Document the prefix-collision caveat (uuid ids make it theoretical).
  4. `storage.delete_discussion(user_id, id)` → False after the get ⇒ treat as deleted (idempotent 404-vs-204 decided below: get-first pattern means False here is a lost race — return success).
- **Router**: `DELETE /discussion/{discussion_id}` → 204 (registered with the fixed-segment ordering discipline — detail GET stays last). Awaiting the engine call; engine raises HTTPException 404 (house style).

## Frontend

- `api/discussion.ts`: `deleteDiscussion(id)`; hook `useDeleteDiscussion` (invalidates `DISCUSSIONS_KEY`).
- **List** (`pages/discussion/index.tsx`): row "…" DropdownMenu (house pattern from workflow cards) with a destructive **Delete** item → `DeleteDialog` (`common.deleteTitle/deleteDescription`) → mutate → row disappears.
- **Detail** (`pages/discussion/detail.tsx`): **Delete** button in the terminal-state action row (beside Rerun/Edit) → same dialog → on success navigate to `/discussion`.
- **404-poll fix**: the detail page's poll flag stays true while `discussion == null` — a missing record (deleted, or dead factory link) re-fetches the 404 every 2s forever. Fix: poll = live && !error (stop on query error; keep the loadFailed Empty).
- i18n en/zh for any new strings (reuse common.delete* keys).

## Non-goals

Bulk delete; cascade into factory tickets (ids are inert strings; the factory UI link degrades to the existing loadFailed view); deleting individual rounds; soft-delete/undo.

## Tests

Router/engine (fakeredis + fake runner): delete COMPLETED → 204, gone from list; delete RUNNING → task settled (registry empty), record gone, participant agents + sessions gone (cascade asserted via storage reads), active index drained; 404 unknown; wrong-owner 404; idempotent double-delete (second → 404); rerun-after-delete unaffected (creates fresh id).
