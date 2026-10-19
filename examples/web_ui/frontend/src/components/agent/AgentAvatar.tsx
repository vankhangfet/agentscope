import { createElement } from 'react';

import { agentDiscClass, agentIcon } from './agentIcons';
import { cn } from '@/lib/utils';

/** Disc sizes: `sm` sits inline in chips and cards, `md` heads a panel. */
const SIZES = {
	sm: { disc: 'size-6', icon: 'size-3.5' },
	md: { disc: 'size-8', icon: 'size-4' },
} as const;

export interface AgentAvatarProps {
	/**
	 * The agent's identity: a roster role name (`product_owner` …
	 * `judge`) or any debater name. Drives the icon and colour, and is
	 * stable for a given name — see `agentIcons.ts`.
	 */
	name: string;
	/**
	 * The speaking halo: a ping ring in the agent's own colour. Purely
	 * visual — the caller owns any "speaking…" labelling.
	 */
	speaking?: boolean;
	/** `sm` inline in chips/cards, `md` heading a panel. */
	size?: 'sm' | 'md';
	/** Native tooltip (house pattern), e.g. the role's stance. */
	title?: string;
}

/**
 * One agent's avatar: a rounded disc wearing a deterministic icon and
 * colour pair — semantic for the 13 roster roles, hashed from the name
 * for anyone else. While `speaking`, a soft ring pings out from the
 * disc in the agent's colour, the `LiveDot` pattern scaled up.
 */
export function AgentAvatar({ name, speaking = false, size = 'md', title }: AgentAvatarProps) {
	const { disc, icon } = SIZES[size];
	return (
		<span
			title={title}
			className={cn(
				'relative flex shrink-0 items-center justify-center rounded-full',
				agentDiscClass(name),
				disc,
			)}
		>
			{speaking && (
				<span
					aria-hidden
					className="absolute -inset-0.5 animate-ping rounded-full bg-current opacity-40"
				/>
			)}
			{/* createElement, because the icon component is chosen per name;
			    relative, so the glyph paints above its own ping halo. */}
			{createElement(agentIcon(name), { className: cn('relative', icon) })}
		</span>
	);
}
