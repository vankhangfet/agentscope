import {
	Bot,
	Brain,
	ClipboardList,
	Cloud,
	Code2,
	Coins,
	Compass,
	Cpu,
	DraftingCompass,
	Feather,
	FileSearch,
	Flame,
	Gauge,
	GitPullRequest,
	Leaf,
	LineChart,
	ListChecks,
	MessageCircle,
	Palette,
	Scale,
	ShieldCheck,
	Sparkles,
	Zap,
	type LucideIcon,
} from 'lucide-react';

/**
 * The agent avatar's icon and colour tables — pure data, kept beside
 * but outside `AgentAvatar.tsx` so pages can reuse the lookups (e.g.
 * colouring an agent's name) without importing a component.
 *
 * Stability contract: these tables and the hash below are a persisted
 * visual identity, not a style choice. A given agent name must resolve
 * to the same icon and colour on every render, for every user. Adding
 * entries is safe; reordering, renaming or rehashing is not — it would
 * silently re-dress every agent in the app. Treat changes like a
 * wire-format change.
 */

/**
 * Semantic icons for the 13 roles `factory_roster()` serves — the 12
 * debate/development roles plus the discussion engine's reserved
 * `judge` key. The factory's phases argue through these very names, so
 * a role's icon lights up wherever the roster data flows. Any other
 * name (a user-named debater) falls back to the hashed pools below.
 */

/**
 * The judge's reserved identity, one source for the whole frontend:
 * `current_speaker`'s judge value and the roster's judge name both
 * carry it. Mirrors the backend's `JUDGE_KEY` in
 * `agentscope/app/_service/discussion/_turns.py`, and is pinned there
 * by `tests/factory_roles_test.py`.
 */
export const JUDGE_KEY = 'judge';

export const ROLE_ICONS: Record<string, LucideIcon> = {
	product_owner: ClipboardList,
	business_analyst: LineChart,
	requirement_reviewer: FileSearch,
	solution_architect: DraftingCompass,
	cloud_architect: Cloud,
	performance_reviewer: Gauge,
	security_reviewer: ShieldCheck,
	cost_reviewer: Coins,
	design_reviewer: Palette,
	plan_lead: ListChecks,
	developer: Code2,
	reviewer: GitPullRequest,
	judge: Scale,
};

/** The tailwind colour families an avatar disc can wear. */
type AvatarColor =
	| 'amber'
	| 'blue'
	| 'cyan'
	| 'emerald'
	| 'fuchsia'
	| 'indigo'
	| 'lime'
	| 'orange'
	| 'rose'
	| 'sky'
	| 'slate'
	| 'teal'
	| 'violet';

/**
 * A curated colour per known role — the roster shows all 13 side by
 * side, and hashing them into 8 fallback buckets would force repeats —
 * with the judge pinned to the amber the discussion views already
 * give it.
 */
const ROLE_COLORS: Record<string, AvatarColor> = {
	product_owner: 'blue',
	business_analyst: 'cyan',
	requirement_reviewer: 'slate',
	solution_architect: 'violet',
	cloud_architect: 'sky',
	performance_reviewer: 'teal',
	security_reviewer: 'rose',
	cost_reviewer: 'lime',
	design_reviewer: 'fuchsia',
	plan_lead: 'indigo',
	developer: 'emerald',
	reviewer: 'orange',
	judge: 'amber',
};

/** The hashed fallback pool for user-named debaters: 10 icons. */
const FALLBACK_ICONS: readonly LucideIcon[] = [
	Bot,
	Brain,
	Cpu,
	Sparkles,
	Zap,
	Feather,
	Flame,
	Leaf,
	Compass,
	MessageCircle,
];

/** The hashed fallback colours: 8 families, distinct per name. */
const FALLBACK_COLORS: readonly AvatarColor[] = [
	'blue',
	'emerald',
	'violet',
	'rose',
	'amber',
	'cyan',
	'fuchsia',
	'teal',
];

/**
 * The disc classes per colour, in the badges' light/dark discipline
 * (100/700 light, 950/300 dark). Written out in full — tailwind only
 * emits classes that appear literally in source.
 */
const DISC_CLASSES: Record<AvatarColor, string> = {
	amber: 'bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300',
	blue: 'bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
	cyan: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-950 dark:text-cyan-300',
	emerald: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300',
	fuchsia: 'bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-950 dark:text-fuchsia-300',
	indigo: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300',
	lime: 'bg-lime-100 text-lime-700 dark:bg-lime-950 dark:text-lime-300',
	orange: 'bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300',
	rose: 'bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300',
	sky: 'bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300',
	slate: 'bg-slate-100 text-slate-700 dark:bg-slate-950 dark:text-slate-300',
	teal: 'bg-teal-100 text-teal-700 dark:bg-teal-950 dark:text-teal-300',
	violet: 'bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300',
};

/** The name-colour companion of each disc: just the text half. */
const TEXT_CLASSES: Record<AvatarColor, string> = {
	amber: 'text-amber-700 dark:text-amber-300',
	blue: 'text-blue-700 dark:text-blue-300',
	cyan: 'text-cyan-700 dark:text-cyan-300',
	emerald: 'text-emerald-700 dark:text-emerald-300',
	fuchsia: 'text-fuchsia-700 dark:text-fuchsia-300',
	indigo: 'text-indigo-700 dark:text-indigo-300',
	lime: 'text-lime-700 dark:text-lime-300',
	orange: 'text-orange-700 dark:text-orange-300',
	rose: 'text-rose-700 dark:text-rose-300',
	sky: 'text-sky-700 dark:text-sky-300',
	slate: 'text-slate-700 dark:text-slate-300',
	teal: 'text-teal-700 dark:text-teal-300',
	violet: 'text-violet-700 dark:text-violet-300',
};

/**
 * FNV-1a over the name — the same hash `avatarTint` uses, kept because
 * it spreads adjacent names across the pool instead of clustering
 * them the way a plain char-code sum does. `>>> 0` pins the result to
 * an unsigned index for every input.
 */
function fnv1a(name: string): number {
	let hash = 0x811c9dc5;
	for (let i = 0; i < name.length; i++) {
		hash ^= name.charCodeAt(i);
		hash = Math.imul(hash, 0x01000193);
	}
	return hash >>> 0;
}

/** An agent's icon: its role's semantic icon, else a hashed fallback. */
export function agentIcon(name: string): LucideIcon {
	return ROLE_ICONS[name] ?? FALLBACK_ICONS[fnv1a(name) % FALLBACK_ICONS.length]!;
}

/** An agent's colour family: its role's curated colour, else hashed. */
function agentColor(name: string): AvatarColor {
	return ROLE_COLORS[name] ?? FALLBACK_COLORS[fnv1a(name) % FALLBACK_COLORS.length]!;
}

/** The avatar disc classes for an agent — `AgentAvatar` wears these. */
export function agentDiscClass(name: string): string {
	return DISC_CLASSES[agentColor(name)];
}

/**
 * The text colour matching an agent's disc — for colouring the agent's
 * name beside its avatar (e.g. on an argument card).
 */
export function agentTextClass(name: string): string {
	return TEXT_CLASSES[agentColor(name)];
}
