import { format } from 'date-fns';
import {
	ArrowLeft,
	Ban,
	Check,
	ChevronDown,
	Code2,
	DraftingCompass,
	Factory,
	Flag,
	History,
	ListChecks,
	MessagesSquare,
	ScrollText,
	UsersRound,
	X,
} from 'lucide-react';
import * as React from 'react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { FactoryModeBadge } from './components/FactoryModeBadge';
import { FactoryPhaseBadge } from './components/FactoryPhaseBadge';
import { FactoryPhaseTimeline } from './components/FactoryPhaseTimeline';
import { FactoryStatusBadge } from './components/FactoryStatusBadge';
import { FACTORY_TERMINAL_STATUSES } from '@/api';
import type {
	FactoryDecision,
	FactoryPhase,
	FactoryRoleGroupView,
	FactoryRoleView,
	FactoryTask,
	FactoryTaskStatus,
} from '@/api';
import { AgentAvatar } from '@/components/agent/AgentAvatar';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Collapsible,
	CollapsibleContent,
	CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import {
	useApproveFactoryTicket,
	useCancelFactoryTicket,
	useFactoryRoster,
	useFactoryTicket,
	useRejectFactoryTicket,
} from '@/hooks/useFactoryTickets';
import { useTranslation } from '@/i18n/useI18n';

/** A develop-task status chip, in the status badges' colour pairs. */
const TASK_STATUS_CLASSES: Record<FactoryTaskStatus, string> = {
	PENDING: 'bg-muted text-muted-foreground',
	RUNNING: 'bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
	DONE: 'bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300',
	FAILED: 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300',
};

function TaskStatusBadge({ status }: { status: FactoryTaskStatus }) {
	const { t } = useTranslation();
	return (
		<Badge className={TASK_STATUS_CLASSES[status]}>
			{t(`factory.taskStatus.${status}`)}
		</Badge>
	);
}

/** The "open this phase's discussion" link, on the artifacts that have one. */
function DiscussionLink({ discussionId }: { discussionId: string }) {
	const { t } = useTranslation();
	return (
		<Button variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs" asChild>
			<Link to={`/discussion/${discussionId}`}>
				<MessagesSquare className="size-3.5" />
				{t('factory.detail.viewDiscussion')}
			</Link>
		</Button>
	);
}

/**
 * One phase's role chips: the group's roles as avatar-chips — each
 * role's `AgentAvatar` (its semantic icon, since the roster names are
 * the avatar's role keys) beside its title, carrying its stance as a
 * native tooltip. The full stances live one section down, in the agent
 * roster panel.
 */
function RoleChips({ roles }: { roles: FactoryRoleView[] }) {
	return (
		<div className="mt-2 flex flex-wrap gap-1.5">
			{roles.map((role) => (
				<span
					key={role.name}
					title={role.stance}
					className="flex items-center gap-1.5 rounded-full border border-border py-0.5 pr-2.5 pl-1 text-[11px] text-muted-foreground"
				>
					<AgentAvatar name={role.name} size="sm" />
					{role.title}
				</span>
			))}
		</div>
	);
}

/**
 * The "Agent roster" panel: every group with its description, each
 * role's full stance, and the judge both debates share. Collapsed by
 * default — the phase headers already carry the chips; this is where
 * the curious reader unfolds the whole team.
 */
function AgentRosterPanel({
	groups,
	judge,
	error,
}: {
	groups: FactoryRoleGroupView[];
	judge: FactoryRoleView | null;
	error: Error | null;
}) {
	const { t } = useTranslation();
	return (
		<section className="rounded-lg border border-border p-4">
			<Collapsible>
				<CollapsibleTrigger className="flex w-full items-center justify-between gap-2 text-sm font-semibold">
					<span className="flex items-center gap-2">
						<UsersRound className="size-4 text-muted-foreground" />
						{t('factory.roster.panelTitle')}
					</span>
					<ChevronDown className="size-4 text-muted-foreground transition-transform data-[state=open]:rotate-180" />
				</CollapsibleTrigger>
				<CollapsibleContent>
					{error != null ? (
						<p className="mt-3 text-xs text-muted-foreground">
							{t('factory.roster.loadFailed')}
						</p>
					) : (
						<div className="mt-3 space-y-4">
							{groups.map((group) => (
								<div key={group.phase} className="space-y-1.5">
									<div className="flex items-center gap-2">
										<span className="text-xs font-semibold">{group.title}</span>
										<span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
											{group.roles.length}
										</span>
									</div>
									<p className="text-xs text-muted-foreground">{group.description}</p>
									<div className="space-y-2">
										{group.roles.map((role) => (
											<div key={role.name} className="flex items-start gap-2">
												<AgentAvatar name={role.name} size="sm" />
												<div className="min-w-0">
													<div className="text-xs font-medium">{role.title}</div>
													<p className="text-xs leading-relaxed break-words whitespace-pre-wrap text-muted-foreground">
														{role.stance}
													</p>
												</div>
											</div>
										))}
									</div>
								</div>
							))}
							{judge != null && (
								<div className="space-y-1.5 border-t border-border pt-3">
									<div className="flex items-center gap-2">
										<AgentAvatar name={judge.name} size="sm" />
										<span className="text-xs font-semibold">{judge.title}</span>
									</div>
									<p className="text-xs leading-relaxed break-words whitespace-pre-wrap text-muted-foreground">
										{judge.stance}
									</p>
									<p className="text-[11px] text-muted-foreground">
										{t('factory.roster.judgeShared', { title: judge.title })}
									</p>
								</div>
							)}
						</div>
					)}
				</CollapsibleContent>
			</Collapsible>
		</section>
	);
}

/**
 * One phase's artifact panel: the section heading, the roles that run
 * the phase (when the roster has loaded), the discussion that produced
 * it (when one is recorded) and the artifact text — or the note that
 * the phase has not concluded yet.
 */
function ArtifactSection({
	icon: Icon,
	title,
	artifact,
	discussionId,
	emptyHint,
	roles,
}: {
	icon: React.ComponentType<{ className?: string }>;
	title: string;
	artifact: string | null;
	discussionId: string | undefined;
	emptyHint: string;
	roles?: FactoryRoleView[];
}) {
	return (
		<section className="rounded-lg border border-border p-4">
			<div className="flex items-center justify-between gap-2">
				<div className="flex items-center gap-2 text-sm font-semibold">
					<Icon className="size-4 text-muted-foreground" />
					{title}
				</div>
				{discussionId != null && <DiscussionLink discussionId={discussionId} />}
			</div>
			{roles != null && roles.length > 0 && <RoleChips roles={roles} />}
			{artifact != null ? (
				<p className="mt-2 text-sm leading-relaxed break-words whitespace-pre-wrap">
					{artifact}
				</p>
			) : (
				<p className="mt-2 text-sm text-muted-foreground italic">{emptyHint}</p>
			)}
		</section>
	);
}

/** One develop task: title, assignee, own status, collapsible output. */
function TaskItem({ task }: { task: FactoryTask }) {
	const { t } = useTranslation();
	return (
		<div className="flex flex-col gap-2 rounded-lg border border-border bg-background/50 p-3">
			<div className="flex flex-wrap items-center justify-between gap-2">
				<span className="min-w-0 truncate text-xs font-semibold">{task.title}</span>
				<TaskStatusBadge status={task.status} />
			</div>
			{task.description !== '' && (
				<p className="text-[13px] leading-relaxed break-words whitespace-pre-wrap text-text-secondary">
					{task.description}
				</p>
			)}
			<div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
				{task.assignee !== '' && (
					<span>
						{t('factory.detail.assignee')}: {task.assignee}
					</span>
				)}
			</div>
			{task.output != null && task.output !== '' && (
				<Collapsible>
					<CollapsibleTrigger className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
						<MessagesSquare className="size-3" />
						{t('factory.detail.taskOutput')}
					</CollapsibleTrigger>
					<CollapsibleContent>
						<pre className="mt-2 max-h-64 overflow-y-auto rounded bg-muted/50 p-2 font-mono text-[11px] leading-relaxed break-words whitespace-pre-wrap text-text-secondary">
							{task.output}
						</pre>
					</CollapsibleContent>
				</Collapsible>
			)}
		</div>
	);
}

/** One decision-log entry, in append order (newest last). */
function DecisionItem({ decision }: { decision: FactoryDecision }) {
	const { t } = useTranslation();
	return (
		<div className="flex flex-col gap-1.5 border-l-2 border-border py-1 pl-4">
			<div className="flex flex-wrap items-center gap-2">
				<FactoryPhaseBadge phase={decision.phase} />
				{decision.consensus ? (
					<Badge className="bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300">
						{t('factory.detail.consensus')}
					</Badge>
				) : (
					<Badge variant="outline" className="text-muted-foreground">
						{t('factory.detail.noConsensus')}
					</Badge>
				)}
				<span className="font-mono text-[11px] text-muted-foreground">
					{format(new Date(decision.created_at), 'yyyy-MM-dd HH:mm:ss')}
				</span>
				{decision.discussion_id != null && (
					<DiscussionLink discussionId={decision.discussion_id} />
				)}
			</div>
			<p className="text-[13px] leading-relaxed break-words whitespace-pre-wrap">
				{decision.conclusion}
			</p>
			{decision.note !== '' && (
				<p className="rounded bg-muted/50 p-2 text-xs leading-relaxed break-words whitespace-pre-wrap text-muted-foreground">
					{t('factory.detail.gateNote')}: {decision.note}
				</p>
			)}
		</div>
	);
}

/**
 * The live ticket view: header (status, mode, cancel), the three-phase
 * timeline with its gates, the gate bar while the ticket waits for a
 * human verdict, one artifact section per phase and the decision log.
 *
 * Liveness: plain polling — 5s while the ticket sits in any non-
 * terminal state (WAITING_HUMAN included: answering the gate needs the
 * fresh state to land), stopping once a fetch reports a terminal
 * status.
 */
export function FactoryTicketDetailPage() {
	const { t } = useTranslation();
	const { ticketId = '' } = useParams<{ ticketId: string }>();
	const [confirmCancel, setConfirmCancel] = React.useState(false);
	const [confirmReject, setConfirmReject] = React.useState(false);
	const [note, setNote] = React.useState('');
	const cancel = useCancelFactoryTicket();
	const approve = useApproveFactoryTicket();
	const reject = useRejectFactoryTicket();

	// Poll only while the ticket is live: the effect flips the flag on
	// every fetch, and the final fetch that sees a terminal status is
	// itself the last poll.
	const [poll, setPoll] = React.useState(true);
	const { ticket, error } = useFactoryTicket(ticketId || null, {
		refetchInterval: poll ? 5000 : false,
	});
	React.useEffect(() => {
		setPoll(ticket == null || !FACTORY_TERMINAL_STATUSES.includes(ticket.status));
	}, [ticket]);

	// The team that runs the phases — a static catalog, so one cached
	// fetch backs the phase chips and the roster panel alike.
	const roster = useFactoryRoster();
	const rolesFor = (phase: FactoryPhase) =>
		roster.groups.find((group) => group.phase === phase)?.roles;

	// ─── Loading / error shell ───────────────────────────────────────────

	if (!ticket) {
		return (
			<div className="flex size-full p-2">
				<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
					<header className="flex items-center gap-2 border-b px-4 py-3">
						<Button variant="ghost" size="icon-sm" asChild>
							<Link to="/factory" aria-label={t('factory.detail.back')}>
								<ArrowLeft />
							</Link>
						</Button>
						<span className="font-mono text-sm text-muted-foreground">{ticketId}</span>
					</header>
					<div className="flex min-h-0 flex-1 items-center justify-center">
						{error ? (
							<Empty className="border-none py-10">
								<EmptyHeader>
									<EmptyMedia variant="icon">
										<Factory />
									</EmptyMedia>
									<EmptyTitle>{t('factory.detail.loadFailed')}</EmptyTitle>
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

	const live = !FACTORY_TERMINAL_STATUSES.includes(ticket.status);
	const discussions = ticket.discussions;
	const developRoles = rolesFor('DEVELOP');

	// Answering the gate with the note typed so far; both mutations
	// invalidate the factory views, so the page repaints on its own.
	const handleApprove = async () => {
		if (approve.isPending) return;
		try {
			await approve.mutateAsync({ ticketId, note: note.trim() });
			toast.success(t('factory.detail.gate.approved'));
			setNote('');
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	const handleCancel = async () => {
		try {
			await cancel.mutateAsync(ticketId);
			toast.success(t('factory.detail.cancelled'));
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<header className="border-b px-4 py-3">
					<div className="flex items-center gap-2">
						<Button variant="ghost" size="icon-sm" asChild>
							<Link to="/factory" aria-label={t('factory.detail.back')}>
								<ArrowLeft />
							</Link>
						</Button>
						<div className="min-w-0 flex-1 truncate text-sm font-semibold">
							{ticket.title}
						</div>
						<FactoryModeBadge mode={ticket.mode} />
						<FactoryPhaseBadge phase={ticket.phase} />
						<FactoryStatusBadge status={ticket.status} />
						{live && (
							<Button variant="outline" size="sm" onClick={() => setConfirmCancel(true)}>
								<Ban />
								{t('factory.detail.cancel')}
							</Button>
						)}
					</div>
					<div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
						<span className="font-mono">{ticketId}</span>
						<span>
							{t('factory.detail.trigger')}: {ticket.trigger}
						</span>
						{ticket.workspace_id != null && (
							<span className="font-mono">
								{t('factory.detail.workspace')}: {ticket.workspace_id}
							</span>
						)}
						<span>
							{t('factory.detail.finished')}:{' '}
							{ticket.finished_at
								? format(new Date(ticket.finished_at), 'yyyy-MM-dd HH:mm:ss')
								: '—'}
						</span>
					</div>
					{ticket.status === 'FAILED' && ticket.error && (
						<p className="mt-2 rounded bg-destructive/10 p-2 font-mono text-[11px] leading-relaxed break-words text-destructive">
							{ticket.error}
						</p>
					)}
				</header>

				<div className="flex-1 space-y-5 overflow-y-auto px-4 py-4">
					<FactoryPhaseTimeline phase={ticket.phase} status={ticket.status} />

					{ticket.status === 'WAITING_HUMAN' && (
						<section className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-4">
							<div className="flex items-center gap-2 text-sm font-semibold text-amber-700 dark:text-amber-400">
								<Flag className="size-4" />
								{t('factory.detail.gate.title')}
							</div>
							<p className="mt-1 text-xs text-muted-foreground">
								{t('factory.detail.gate.description')}
							</p>
							<div className="mt-3 flex flex-col gap-2 sm:flex-row">
								<Input
									value={note}
									className="flex-1"
									placeholder={t('factory.detail.gate.notePlaceholder')}
									aria-label={t('factory.detail.gate.noteLabel')}
									disabled={approve.isPending || reject.isPending}
									onChange={(e) => setNote(e.target.value)}
								/>
								<Button
									disabled={approve.isPending}
									onClick={() => void handleApprove()}
								>
									{approve.isPending ? (
										<Spinner className="size-3.5" />
									) : (
										<Check />
									)}
									{t('factory.detail.gate.approve')}
								</Button>
								<Button
									variant="destructive"
									disabled={reject.isPending}
									onClick={() => setConfirmReject(true)}
								>
									<X />
									{t('factory.detail.gate.reject')}
								</Button>
							</div>
						</section>
					)}

					<ArtifactSection
						icon={ListChecks}
						title={t('factory.phase.REQUIREMENTS')}
						artifact={ticket.requirements}
						discussionId={discussions.REQUIREMENTS}
						emptyHint={t('factory.detail.noArtifact')}
						roles={rolesFor('REQUIREMENTS')}
					/>

					<ArtifactSection
						icon={DraftingCompass}
						title={t('factory.phase.ARCHITECTURE')}
						artifact={ticket.architecture}
						discussionId={discussions.ARCHITECTURE}
						emptyHint={t('factory.detail.noArtifact')}
						roles={rolesFor('ARCHITECTURE')}
					/>

					<section className="rounded-lg border border-border p-4">
						<div className="flex items-center justify-between gap-2">
							<div className="flex items-center gap-2 text-sm font-semibold">
								<Code2 className="size-4 text-muted-foreground" />
								{t('factory.phase.DEVELOP')}
							</div>
							{discussions.DEVELOP != null && (
								<DiscussionLink discussionId={discussions.DEVELOP} />
							)}
						</div>
						{developRoles != null && developRoles.length > 0 && (
							<RoleChips roles={developRoles} />
						)}
						{ticket.tasks.length === 0 ? (
							<p className="mt-2 text-sm text-muted-foreground italic">
								{t('factory.detail.noTasks')}
							</p>
						) : (
							<div className="mt-3 space-y-3">
								{ticket.tasks.map((task, i) => (
									<TaskItem key={`${task.title}-${i}`} task={task} />
								))}
							</div>
						)}
						{ticket.review != null && (
							<div className="mt-3 rounded-lg border border-green-600/30 bg-green-500/5 p-3">
								<div className="flex items-center gap-2 text-sm font-semibold text-green-700 dark:text-green-400">
									<ScrollText className="size-4" />
									{t('factory.detail.review')}
								</div>
								<p className="mt-2 text-sm leading-relaxed break-words whitespace-pre-wrap">
									{ticket.review}
								</p>
							</div>
						)}
					</section>

					{!roster.loading && (
						<AgentRosterPanel
							groups={roster.groups}
							judge={roster.judge}
							error={roster.error}
						/>
					)}

					<section className="rounded-lg border border-border p-4">
						<div className="flex items-center gap-2 text-sm font-semibold">
							<History className="size-4 text-muted-foreground" />
							{t('factory.detail.decisionLog')}
						</div>
						{ticket.decisions.length === 0 ? (
							<p className="mt-2 text-sm text-muted-foreground italic">
								{t('factory.detail.noDecisions')}
							</p>
						) : (
							<div className="mt-3 space-y-3">
								{ticket.decisions.map((decision, i) => (
									<DecisionItem key={`${decision.phase}-${i}`} decision={decision} />
								))}
							</div>
						)}
					</section>
				</div>
			</main>

			<DeleteDialog
				open={confirmCancel}
				onOpenChange={setConfirmCancel}
				title={t('factory.detail.cancelTitle')}
				description={t('factory.detail.cancelDescription')}
				confirmLabel={t('factory.detail.cancelConfirm')}
				onConfirm={handleCancel}
			/>

			{/* Reject needs a second look: it throws away the phase's work
			    and re-runs it from scratch. */}
			<DeleteDialog
				open={confirmReject}
				onOpenChange={setConfirmReject}
				title={t('factory.detail.gate.rejectTitle')}
				description={t('factory.detail.gate.rejectDescription')}
				confirmLabel={t('factory.detail.gate.rejectConfirm')}
				onConfirm={async () => {
					try {
						await reject.mutateAsync({ ticketId, note: note.trim() });
						toast.success(t('factory.detail.gate.rejected'));
						setNote('');
					} catch {
						// The API client already toasted the failure detail.
					}
				}}
			/>
		</div>
	);
}
