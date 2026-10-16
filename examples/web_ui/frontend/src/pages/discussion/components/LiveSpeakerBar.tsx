import { Loader2, Scale } from 'lucide-react';

import type { DiscussionData } from '@/api';
import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import { Skeleton } from '@/components/ui/skeleton';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';
import { avatarTint } from '@/utils/common';

/** The judge's reserved `current_speaker` value, per the backend. */
const JUDGE = 'judge';

/**
 * A pulsing "live" dot — a filled core under an expanding ring, the
 * conventional recording/live indicator.
 */
function LiveDot({ className }: { className?: string }) {
	return (
		<span className={cn('relative flex size-2.5 shrink-0', className)}>
			<span className="absolute inline-flex size-full animate-ping rounded-full bg-current opacity-60" />
			<span className="relative inline-flex size-2.5 rounded-full bg-current" />
		</span>
	);
}

/**
 * The live speaker bar ("thanh nói chuyện") plus the waiting frame
 * ("khung chờ") for whoever currently holds the floor.
 *
 * Reads `current_speaker`: a debater's name while that debater argues,
 * `"judge"` while the judge scores a round or synthesizes the
 * conclusion, and `null` when nothing speaks. The bar announces the
 * speaker with a live indicator; the frame below previews what is being
 * written — a skeleton argument card for a debater, an "evaluating"
 * strip for the judge (the arbitrator / "trọng tài"). It renders
 * nothing once the floor is empty, so a terminal discussion shows only
 * its timeline and conclusion.
 */
export function LiveSpeakerBar({ discussion }: { discussion: DiscussionData }) {
	const { t } = useTranslation();
	const speaker = discussion.current_speaker;

	// Nothing holds the floor: terminal states and the brief gaps
	// between turns. The timeline below carries the story, so the bar
	// steps aside rather than flashing an empty frame.
	if (!speaker) return null;

	const isJudge = speaker === JUDGE;

	return (
		<section
			className={cn(
				'flex flex-col gap-3 rounded-xl border p-4',
				isJudge
					? 'border-amber-500/40 bg-amber-500/5'
					: 'border-blue-500/40 bg-blue-500/5',
			)}
			aria-live="polite"
		>
			{/* The bar: who is speaking, with a live pulse. */}
			<div className="flex items-center gap-3">
				{isJudge ? (
					<span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400">
						<Scale className="size-4" />
					</span>
				) : (
					<Avatar className="size-8 shrink-0">
						<AvatarFallback style={avatarTint(speaker)} className="text-xs font-semibold">
							{speaker.slice(0, 1).toUpperCase()}
						</AvatarFallback>
					</Avatar>
				)}

				<div className="min-w-0 flex-1">
					<div className="flex items-center gap-1.5 text-sm font-semibold">
						<span className="min-w-0 truncate">
							{isJudge ? t('discussion.live.judgeName') : speaker}
						</span>
						{isJudge && (
							<span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-600 dark:text-amber-400">
								{t('discussion.live.judgeTag')}
							</span>
						)}
					</div>
					<div className="truncate text-xs text-muted-foreground">
						{isJudge
							? t('discussion.live.judgeEvaluating')
							: t('discussion.live.debaterSpeaking')}
					</div>
				</div>

				<span
					className={cn(
						'flex items-center gap-1.5 text-xs font-medium',
						isJudge ? 'text-amber-600 dark:text-amber-400' : 'text-blue-600 dark:text-blue-400',
					)}
				>
					<LiveDot />
					{t('discussion.live.live')}
				</span>
			</div>

			{/* The waiting frame: a preview of what is being produced. */}
			{isJudge ? (
				<div className="flex items-center gap-2 rounded-lg border border-amber-500/20 bg-background/60 p-3 text-xs text-muted-foreground">
					<Loader2 className="size-3.5 shrink-0 animate-spin" />
					{t('discussion.live.judgeWaiting')}
				</div>
			) : (
				<div className="flex flex-col gap-2 rounded-lg border border-blue-500/20 bg-background/60 p-3">
					<div className="flex items-center gap-2 text-xs text-muted-foreground">
						<Loader2 className="size-3.5 shrink-0 animate-spin" />
						{t('discussion.live.debaterWaiting')}
					</div>
					{/* Skeleton lines standing in for the argument being written. */}
					<div className="flex flex-col gap-1.5">
						<Skeleton className="h-3 w-full" />
						<Skeleton className="h-3 w-[92%]" />
						<Skeleton className="h-3 w-[70%]" />
					</div>
				</div>
			)}
		</section>
	);
}
