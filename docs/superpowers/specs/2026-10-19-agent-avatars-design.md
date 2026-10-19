# Agent Avatars & Live Animations — Design (v1)

- **Date:** 2026-10-19
- **Status:** Approved (full option: shared AgentAvatar + discussion live animations + factory surfaces)
- **Goal:** Make multi-agent discussions feel alive: every agent gets a recognizable icon, new arguments animate in, and the current speaker is visibly "speaking".

## `AgentAvatar` (new shared component, `components/`)

- Deterministic per agent name: icon + color pair from a fixed palette (stable across renders/users).
- Semantic icons for the 13 known roles: product_owner `ClipboardList`, business_analyst `LineChart`, requirement_reviewer `FileSearch`, solution_architect `DraftingCompass`, cloud_architect `Cloud`, performance_reviewer `Gauge`, security_reviewer `ShieldCheck`, cost_reviewer `Coins`, design_reviewer `Palette`, plan_lead `ListChecks`, developer `Code2`, reviewer `GitPullRequest`, judge `Scale`.
- Arbitrary debater names (user-named) hash into a fixed icon/color fallback table.
- Props: `name`, `size`, optional `speaking?: boolean` (pulse ring), optional `label` tooltip. Color pairs follow house light/dark discipline (100/700 + 950/300).

## Discussion detail (live view)

- Argument cards: AgentAvatar + colored agent name; **new arguments animate in** (tailwindcss-animate `animate-in fade-in slide-in-from-bottom`, shadcn's existing plugin).
- **Speaking indicator**: the agent holding the discussion's `current_speaker` gets a pulsing ring + "speaking…" chip; judge turn pulses the `Scale` avatar. Only while the discussion is RUNNING.
- Round/turn separation visually clearer (subtle dividers/headers already house-styled — keep, just accent).

## Factory tie-in

- Factory detail phase chips, "Agent roster" panel, and Create dialog "Agents involved" all render the same `AgentAvatar` (role names already flow from the roster API, so semantic icons light up automatically).

## Non-goals

WebGL/confetti-level effects; per-message typing simulation; avatar upload; changes to backend data (current_speaker already exists).

## i18n

en/zh for every new string ("speaking…", tooltips).
