import { format } from 'date-fns';
import { ArrowLeft, Ban, MessagesSquare, Pencil, RotateCcw, ScrollText, Star } from 'lucide-react';
import * as React from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { DiscussionSetupPanel } from './components/DiscussionSetupPanel';
import { DiscussionStatusBadge } from './components/DiscussionStatusBadge';
import { LiveSpeakerBar } from './components/LiveSpeakerBar';
import { DISCUSSION_TERMINAL_STATUSES } from '@/api';
import type { RoundArgument, RoundEvaluation } from '@/api';
import { AgentAvatar } from '@/components/agent/AgentAvatar';
import { agentTextClass } from '@/components/agent/agentIcons';
import { CreateDiscussionDialog } from '@/components/dialog/CreateDiscussionDialog';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty';
import { Spinner } from '@/components/ui/spinner';
import { useCancelDiscussion, useDiscussion, useRerunDiscussion } from '@/hooks/useDiscussions';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

/** The judge's 1-5 strength score as five stars, filled up to the score. */
function StrengthStars({ strength }: { strength: number | null }) {
	const { t } = useTranslation();
	if (strength == null) return null;
	return (
		<span
			className="flex items-center gap-0.5"
			role="img"
			aria-label={t('discussion.detail.strength', { score: strength })}
		>
			{Array.from({ length: 5 }).map((_, i) => (
				<Star
					key={i}
					className={cn(
						'size-3.5',
						i < strength
							? 'fill-amber-400 text-amber-400'
							: 'text-muted-foreground/40',
					)}
				/>
			))}
		</span>
	);
}

/**
 * One debater's argument card, with the judge's verdict on it. The
 * debater wears its avatar and its colour; `animate` plays the entrance
 * only for cards that arrived live (see the snapshot in the page).
 */
function ArgumentCard({
	argument,
	animate = false,
}: {
	argument: RoundArgument;
	animate?: boolean;
}) {
	const { t } = useTranslation();
	return (
		<div
			className={cn(
				'flex flex-col gap-2 rounded-lg border border-border bg-background/50 p-3',
				animate && 'animate-in fade-in slide-in-from-bottom-2 duration-300',
			)}
		>
			<div className="flex items-center justify-between gap-2">
				<span className="flex min-w-0 items-center gap-1.5">
					<AgentAvatar name={argument.debater} size="sm" />
					<span
						className={cn(
							'min-w-0 truncate text-xs font-semibold',
							agentTextClass(argument.debater),
						)}
					>
						{argument.debater}
					</span>
				</span>
				<StrengthStars strength={argument.strength} />
			</div>
			{/* A rebutted argument stays readable under the strike — the
			 * judge's point is that a later one beat it, not that it
			 * vanished. */}
			<p
				className={cn(
					'text-[13px] leading-relaxed break-words whitespace-pre-wrap',
					argument.rebutted === true &&
						'text-muted-foreground line-through decoration-muted-foreground/50',
				)}
			>
				{argument.text}
			</p>
			{(argument.rebutted === true || argument.note !== '') && (
				<div className="mt-auto flex flex-wrap items-center gap-2 pt-1 text-xs">
					{argument.rebutted === true && (
						<Badge variant="outline" className="text-muted-foreground">
							{t('discussion.detail.rebutted')}
						</Badge>
					)}
					{argument.note !== '' && (
						<span className="text-muted-foreground">{argument.note}</span>
					)}
				</div>
			)}
		</div>
	);
}

/**
 * One round on the timeline: the argument cards, then the verdict.
 * `isFresh` flags the cards that arrived after the page's first paint,
 * so only those animate in.
 */
function RoundSection({
	round,
	isFresh,
}: {
	round: RoundEvaluation;
	isFresh: (debater: string, index: number) => boolean;
}) {
	const { t } = useTranslation();
	return (
		<section className="space-y-2 border-l-2 border-border pl-4">
			<div className="flex flex-wrap items-center gap-2">
				<span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
					{t('discussion.detail.round', { round: round.round })}
				</span>
				{round.consensus ? (
					<Badge className="bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300">
						{t('discussion.detail.consensus')}
					</Badge>
				) : (
					<Badge variant="outline" className="text-muted-foreground">
						{t('discussion.detail.noConsensus')}
					</Badge>
				)}
			</div>
			<div className="grid gap-3 md:grid-cols-2">
				{round.arguments.map((argument, i) => (
					<ArgumentCard
						key={`${argument.debater}-${i}`}
						argument={argument}
						animate={isFresh(argument.debater, i)}
					/>
				))}
			</div>
			{round.summary !== '' && (
				<p className="rounded-lg bg-muted/50 p-3 text-xs leading-relaxed break-words text-muted-foreground">
					{round.summary}
				</p>
			)}
		</section>
	);
}

/**
 * The live debate view: header (status, cancel), a live speaker bar
 * naming who holds the floor right now, the rounds timeline with the
 * in-progress round's waiting frame, and the conclusion panel once the
 * judge synthesizes it.
 *
 * Liveness: plain polling — 2s while the discussion is PENDING or
 * RUNNING, stopping once a fetch reports a terminal status (the SSE
 * stream of the run view follows later, per the design's non-goals).
 */
export function DiscussionDetailPage() {
	const { t } = useTranslation();
	const { discussionId = '' } = useParams<{ discussionId: string }>();
	const navigate = useNavigate();
	const [confirmCancel, setConfirmCancel] = React.useState(false);
	const [editRerunOpen, setEditRerunOpen] = React.useState(false);
	const cancel = useCancelDiscussion();
	const rerun = useRerunDiscussion();

	// Rerun = clone the config into a fresh discussion server-side, then
	// jump to it. Edit-&-rerun instead opens the create dialog seeded
	// from this discussion, so the user can change the debaters first.
	const handleRerun = async () => {
		if (rerun.isPending) return;
		try {
			const res = await rerun.mutateAsync(discussionId);
			toast.success(t('discussion.rerun.started'));
			navigate(`/discussion/${res.discussion_id}`);
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	// Poll only while the debate is live: the effect flips the flag on
	// every fetch, and the final fetch that sees a terminal status is
	// itself the last poll. 2s keeps the live speaker bar and the
	// in-progress argument feeling close to real time without an SSE
	// stream (that follows later, per the design's non-goals).
	const [poll, setPoll] = React.useState(true);
	const { discussion, error } = useDiscussion(discussionId || null, {
		refetchInterval: poll ? 2000 : false,
	});
	React.useEffect(() => {
		setPoll(
			discussion == null || !DISCUSSION_TERMINAL_STATUSES.includes(discussion.status),
		);
	}, [discussion]);

	// ─── Entrance animation ──────────────────────────────────────────
	//
	// Arguments animate in only when they arrive live: each discussion's
	// first painted data is snapshotted as that discussion's baseline
	// (after the paint, in the effect below), so a COMPLETED — or merely
	// old — discussion lands perfectly still, and anything outside the
	// baseline — the in-progress round's arguments as the poll delivers
	// them — enters with the fade/slide. The animation then plays
	// exactly once: keys are stable (a card is never re-keyed) and the
	// class simply stays on, because CSS runs an entrance when the
	// element mounts, not on every re-render — so a poll that changes
	// nothing re-animates nothing.
	//
	// The baseline is keyed by discussion id because this route's
	// component instance survives rerun and edit-&-rerun, which
	// navigate to a brand-new discussion without unmounting: the new
	// discussion gets its own baseline, so its cards animate as they
	// arrive instead of being swallowed by the old one's keys (a rerun
	// starts with an empty blackboard, so in practice everything it
	// paints arrives live, after its baseline).
	const [baseline, setBaseline] = React.useState<{
		discussionId: string;
		keys: Set<string>;
	} | null>(null);
	React.useEffect(() => {
		// Once per discussion: whatever its first paint showed becomes
		// its baseline. An id mismatch (rerun navigated this instance to
		// a new discussion) re-snapshots from the new discussion's first
		// painted data.
		if (discussion == null || baseline?.discussionId === discussionId) return;
		setBaseline({
			discussionId,
			keys: new Set(
				discussion.rounds.flatMap((round) =>
					round.arguments.map(
						(argument, i) => `${round.round}:${argument.debater}:${i}`,
					),
				),
			),
		});
	}, [discussion, discussionId, baseline]);
	const isFreshArgument = (round: number, debater: string, index: number) =>
		baseline != null &&
		baseline.discussionId === discussionId &&
		!baseline.keys.has(`${round}:${debater}:${index}`);

	// ─── Loading / error shell ───────────────────────────────────────────

	if (!discussion) {
		return (
			<div className="flex size-full p-2">
				<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
					<header className="flex items-center gap-2 border-b px-4 py-3">
						<Button variant="ghost" size="icon-sm" asChild>
							<Link to="/discussion" aria-label={t('discussion.detail.back')}>
								<ArrowLeft />
							</Link>
						</Button>
						<span className="font-mono text-sm text-muted-foreground">{discussionId}</span>
					</header>
					<div className="flex min-h-0 flex-1 items-center justify-center">
						{error ? (
							<Empty className="border-none py-10">
								<EmptyHeader>
									<EmptyMedia variant="icon">
										<MessagesSquare />
									</EmptyMedia>
									<EmptyTitle>{t('discussion.detail.loadFailed')}</EmptyTitle>
									<EmptyDescription>{error.message}</EmptyDescription>
								</EmptyHeader>
							</Empty>
						) : (
							<Spinner className="size-6" />
						)}
					</div>
				</main>
			</div>
		);
	}

	// ─── Live view ───────────────────────────────────────────────────────

	const live = !DISCUSSION_TERMINAL_STATUSES.includes(discussion.status);

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<header className="border-b px-4 py-3">
					<div className="flex items-center gap-2">
						<Button variant="ghost" size="icon-sm" asChild>
							<Link to="/discussion" aria-label={t('discussion.detail.back')}>
								<ArrowLeft />
							</Link>
						</Button>
						<div className="min-w-0 flex-1 truncate text-sm font-semibold">
							{discussion.topic}
						</div>
						<Badge variant="secondary">
							{t('discussion.detail.roundsOf', {
								current: discussion.rounds.length,
								max: discussion.max_rounds,
							})}
						</Badge>
						<DiscussionStatusBadge status={discussion.status} />
						{(discussion.status === 'PENDING' || discussion.status === 'RUNNING') && (
							<Button variant="outline" size="sm" onClick={() => setConfirmCancel(true)}>
								<Ban />
								{t('discussion.detail.cancel')}
							</Button>
						)}
						{/* Rerun / edit are offered once the debate is over —
						    cloning a still-running config would race a record
						    the engine is actively mutating. */}
						{!live && (
							<>
								<Button
									variant="outline"
									size="sm"
									disabled={rerun.isPending}
									onClick={() => void handleRerun()}
								>
									{rerun.isPending ? (
										<Spinner className="size-3.5" />
									) : (
										<RotateCcw />
									)}
									{t('discussion.rerun.action')}
								</Button>
								<Button
									variant="outline"
									size="sm"
									onClick={() => setEditRerunOpen(true)}
								>
									<Pencil />
									{t('discussion.edit.rerun.action')}
								</Button>
							</>
						)}
					</div>
					<div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
						<span className="font-mono">{discussionId}</span>
						<span>
							{t('discussion.detail.trigger')}: {discussion.trigger}
						</span>
						<span>
							{t('discussion.detail.finished')}:{' '}
							{discussion.finished_at
								? format(new Date(discussion.finished_at), 'yyyy-MM-dd HH:mm:ss')
								: '—'}
						</span>
					</div>
					{discussion.status === 'FAILED' && discussion.error && (
						<p className="mt-2 rounded bg-destructive/10 p-2 font-mono text-[11px] leading-relaxed break-words text-destructive">
							{discussion.error}
						</p>
					)}
				</header>

				<div className="flex-1 space-y-5 overflow-y-auto px-4 py-4">
					{live && <LiveSpeakerBar discussion={discussion} />}

					<DiscussionSetupPanel discussion={discussion} />

					{discussion.conclusion != null && (
						<section className="rounded-lg border border-green-600/30 bg-green-500/5 p-4">
							<div className="flex items-center gap-2 text-sm font-semibold text-green-700 dark:text-green-400">
								<ScrollText className="size-4" />
								{t('discussion.detail.conclusion')}
							</div>
							<p className="mt-2 text-sm leading-relaxed break-words whitespace-pre-wrap">
								{discussion.conclusion}
							</p>
						</section>
					)}

					{discussion.rounds.length === 0 ? (
						// The live bar already fills the pre-first-round gap
						// whenever someone holds the floor; only fall back to
						// the empty state when nothing is speaking (a PENDING
						// debate still spinning up, or a terminal one that
						// ended before any round completed).
						discussion.current_speaker ? null : (
							<Empty className="border-none py-10">
								<EmptyHeader>
									<EmptyMedia variant="icon">
										<MessagesSquare />
									</EmptyMedia>
									<EmptyTitle>{t('discussion.detail.noRoundsTitle')}</EmptyTitle>
									<EmptyDescription>
										{live
											? t('discussion.detail.noRoundsLive')
											: t('discussion.detail.noRoundsDone')}
									</EmptyDescription>
								</EmptyHeader>
							</Empty>
						)
					) : (
						discussion.rounds.map((round) => (
							<RoundSection
								key={round.round}
								round={round}
								isFresh={(debater, index) =>
									isFreshArgument(round.round, debater, index)
								}
							/>
						))
					)}
				</div>
			</main>

			<DeleteDialog
				open={confirmCancel}
				onOpenChange={setConfirmCancel}
				title={t('discussion.detail.cancelTitle')}
				description={t('discussion.detail.cancelDescription')}
				confirmLabel={t('discussion.detail.cancelConfirm')}
				onConfirm={async () => {
					try {
						await cancel.mutateAsync(discussionId);
						toast.success(t('discussion.detail.cancelled'));
					} catch {
						// The API client already toasted the failure detail.
					}
				}}
			/>

			{/* Edit & rerun: the create dialog seeded from this discussion.
			    Submitting creates a NEW discussion (the source is never
			    mutated); on success we navigate to it. */}
			<CreateDiscussionDialog
				open={editRerunOpen}
				onOpenChange={setEditRerunOpen}
				initial={discussion}
				onCreated={(newId) => navigate(`/discussion/${newId}`)}
			/>
		</div>
	);
}
