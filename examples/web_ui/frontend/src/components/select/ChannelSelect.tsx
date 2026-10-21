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
 */
export function ChannelSelect({ channels, value, onChange, placeholder, className }: Props) {
	const { t } = useTranslation();
	const enabled = channels.filter((c) => c.enabled);

	return (
		<Select value={value || undefined} onValueChange={onChange}>
			<SelectTrigger className={className}>
				<SelectValue
					placeholder={placeholder ?? t('workflow.builder.config.notify.channelPlaceholder')}
				/>
			</SelectTrigger>
			<SelectContent>
				{enabled.map((channel) => (
					<SelectItem key={channel.id} value={channel.id}>
						<span className="min-w-0 flex-1 truncate">{channel.name ?? channel.id}</span>
						<Badge variant="outline" className="ml-auto shrink-0 px-1 py-0 text-[10px]">
							{channel.channel_type}
						</Badge>
					</SelectItem>
				))}
			</SelectContent>
		</Select>
	);
}
