# Factory Agent Roster & Copy — Design (v1)

- **Date:** 2026-10-18
- **Status:** Approved (full option: API-driven roster + create-dialog preview + detail chips/panel + copy improvements)
- **Goal:** Make the factory's team visible: who will run before creating a ticket, who ran in each phase, and clearer mode/phase copy.

## Problem

The factory runs 12 logical roles plus a judge (all sharing the ticket's model), but the UI shows none of them. Users create tickets without knowing the team, and mode/phase descriptions are thin.

## API

- `GET /factory/roles` → `FactoryRosterResponse`:
  - `groups: list[FactoryRoleGroupView]` — REQUIREMENTS (3), ARCHITECTURE (6), DEVELOP (plan_lead, developer, reviewer; developer covers the per-task `developer_N` sessions)
  - `FactoryRoleGroupView{phase: FactoryPhase, title, description, roles: list[FactoryRoleView]}`
  - `FactoryRoleView{name, title, stance}`; `judge: FactoryRoleView` (shared by both discussion phases)
- **Route order:** `/roles` is registered before `/{ticket_id}` (fixed-segment discipline) so it is never shadowed.
- Single source of truth stays `_roles.py`: existing `ROLE_GROUP`/`to_debaters` untouched; additive structures (role titles, development entries, judge description) + a pure `factory_roster()` that the router serializes.

## UI

- **Create dialog:** collapsible "Agents involved" — chips per group (3 / 6 / 3 + judge), expandable stance per role.
- **Detail:** each phase section shows its roles as chips; an "Agent roster" collapsible panel with full stances.
- **Copy:** mode helpers ("Collaborative — you approve every gate", "Delegated — gates auto-approve until the final review"), per-step captions on the phase timeline, clearer empty states.
- i18n en/zh for every new string.

## Non-goals

Per-role model overrides; editing the roster from the UI; runtime turn-by-turn attribution inside the factory pages (the Discussions page already shows that).
