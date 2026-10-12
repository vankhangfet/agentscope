import { Bug, GitBranch, Plug } from 'lucide-react';
import { useState } from 'react';

import type { IntegrationPreset } from '@/api';
import { AddIntegrationDialog } from '@/components/dialog/AddIntegrationDialog.tsx';
import { useTranslation } from '@/i18n/useI18n';
import { avatarTint } from '@/utils/common';

/**
 * Per-preset glyphs. Lucide dropped its brand icons, so GitLab gets the
 * generic git branch rather than its logo, and Redmine the issue-tracker
 * bug; ids without an entry fall back to the plug.
 */
const PRESET_ICONS: Record<string, typeof Plug> = {
	gitlab: GitBranch,
	redmine: Bug,
};

function PresetIcon({ preset }: { preset: IntegrationPreset }) {
	const Icon = PRESET_ICONS[preset.id] ?? Plug;
	return (
		// Tinted like an avatar fallback, so a preset card and the library
		// row it becomes read as the same thing.
		<span
			className="flex size-7 shrink-0 items-center justify-center rounded-md"
			style={avatarTint(preset.id)}
		>
			<Icon className="size-4" />
		</span>
	);
}

interface Props {
	presets: IntegrationPreset[];
	/** Signals the page to refetch the library after a create. */
	onAdded: () => void;
}

/**
 * The one-click adds: the built-in integration presets, shown above the
 * library list. Needs no hub — clicking one opens the field form straight
 * from the catalog entry.
 */
export function AddIntegrationSection({ presets, onAdded }: Props) {
	const { t } = useTranslation();
	const [adding, setAdding] = useState<IntegrationPreset | null>(null);

	// No catalog, no section — an empty list is a deployment detail, not
	// something to explain on the page.
	if (presets.length === 0) return null;

	return (
		<div className="flex flex-col gap-y-2.5 px-[18px] pt-4">
			<span className="text-xs font-medium text-muted-foreground">
				{t('mcp.integration.sectionLabel')}
			</span>
			<div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-2">
				{presets.map((preset) => (
					<button
						key={preset.id}
						type="button"
						className="flex flex-col items-start gap-y-1.5 rounded-xl border border-border p-3 text-left transition-colors hover:bg-row-hover"
						onClick={() => setAdding(preset)}
					>
						<span className="flex items-center gap-2">
							<PresetIcon preset={preset} />
							<span className="text-sm font-medium">{preset.name}</span>
						</span>
						{/* Two lines reserved, so a one-line description does
						    not make its card shorter than its neighbours. */}
						<span className="line-clamp-2 min-h-8 text-xs text-muted-foreground">
							{preset.description}
						</span>
					</button>
				))}
			</div>

			<AddIntegrationDialog
				preset={adding}
				onOpenChange={(open) => {
					if (!open) setAdding(null);
				}}
				onCreated={onAdded}
			/>
		</div>
	);
}
