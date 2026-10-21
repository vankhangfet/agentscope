import type { ChannelRecord } from '@/api';
import { Badge } from '@/components/ui/badge';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	/** The caller's channels; only enabled ones are offered. */
	channels: ChannelRecord[];
	/** Currently selected channel id, or `null` / `''` when none. */
	value?: string | null;
	/** Fired with the picked channel id. */
	onChange: (channelId: string) => void;
	/** Override the trigger label shown when no channel is selected. */
	placeholder?: string;
	className?: string;
}

/**
 * Channel picker over the shadcn Select primitives: the enabled
 * channels of the list handed in, each option showing its name (falling
 * back to the record id) with a channel-type badge. Callers own the
 * fetch (`useChannels`) so a page with several pickers shares one list.
 *
 * A value pointing outside the offered items — a channel disabled or
 * deleted after it was configured — still resolves to a trigger label:
 * Radix can only label the trigger from a mounted item, so that case is
 * rendered explicitly here, muted and tagged "(disabled)" when the
 * record exists, keeping the stale reference visible instead of an
 * empty trigger.
 */
export function ChannelSelect({ channels, value, onChange, placeholder, className }: Props) {
	const { t } = useTranslation();
	const enabled = channels.filter((c) => c.enabled);
	const id = value || '';
	const selected = channels.find((c) => c.id === id) ?? null;
	// Among the rendered items Radix labels the trigger itself; only
	// an out-of-list value needs explicit children (undefined keeps
	// Radix's default, placeholder included).
	const offered = selected?.enabled === true;
	const staleLabel =
		id !== '' && !offered ? (
			<span className="flex min-w-0 items-center gap-1.5">
				<span className="truncate text-muted-foreground">{selected?.name ?? id}</span>
				{selected !== null && (
					<span className="shrink-0 text-[10px] text-amber-600 dark:text-amber-400">
						{t('workflow.builder.config.notify.channelDisabledTag')}
					</span>
				)}
			</span>
		) : undefined;

	return (
		<Select value={id || undefined} onValueChange={onChange}>
			<SelectTrigger className={className}>
				<SelectValue
					placeholder={placeholder ?? t('workflow.builder.config.notify.channelPlaceholder')}
				>
					{staleLabel}
				</SelectValue>
			</SelectTrigger>
			<SelectContent>
				{enabled.length === 0 ? (
					// AgentSelect's pattern: say why there is nothing to
					// pick instead of opening a dead dropdown.
					<div className="px-2 py-3 text-center text-sm text-muted-foreground">
						<p className="font-medium">
							{t('workflow.builder.config.notify.channelEmptyTitle')}
						</p>
						<p className="mt-1 text-xs">
							{t('workflow.builder.config.notify.channelEmptyDescription')}
						</p>
					</div>
				) : (
					enabled.map((channel) => (
						<SelectItem key={channel.id} value={channel.id}>
							<span className="min-w-0 flex-1 truncate">{channel.name ?? channel.id}</span>
							<Badge variant="outline" className="ml-auto shrink-0 px-1 py-0 text-[10px]">
								{channel.channel_type}
							</Badge>
						</SelectItem>
					))
				)}
			</SelectContent>
		</Select>
	);
}
