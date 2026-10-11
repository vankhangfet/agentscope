import { useQueryClient } from '@tanstack/react-query';
import { format } from 'date-fns';
import { Check, CheckCircle2, History, X } from 'lucide-react';
import * as React from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';

import { RunStatusBadge } from './components/RunStatusBadge';
import type { ApproveVerdict, WorkflowRun, WorkflowRunData } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Empty,
	EmptyContent,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { Item } from '@/components/ui/item';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { useApproveRun, useWorkflowRuns, WORKFLOW_RUNS_KEY } from '@/hooks/useWorkflowRuns';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

/** Preview cap: enough of the upstream output to judge an approval. */
const PREVIEW_LIMIT = 300;

/**
 * The most informative parked-run preview: the text of the last
 * SUCCEEDED agent node (in graph order), trimmed. Falls back to null
 * when no agent has produced text yet.
 */
function previewText(data: WorkflowRunData): string | null {
	const agents = data.graph.nodes.filter(
		(node) =>
			node.config.type === 'agent' &&
			data.node_states[node.id]?.status === 'SUCCEEDED',
	);
	for (let i = agents.length - 1; i >= 0; i -= 1) {
		const text = data.context[agents[i].id]?.text;
		if (typeof text === 'string' && text.trim() !== '') {
			const trimmed = text.trim();
			return trimmed.length > PREVIEW_LIMIT
				? `${trimmed.slice(0, PREVIEW_LIMIT)}…`
				: trimmed;
		}
	}
	return null;
}

/**
 * One parked approval node. A run waiting on several nodes renders
 * several cards, so parallel approvals stay individually answerable;
 * answering invalidates the run lists and the card leaves with the
 * refetch.
 */
function ApprovalCard({ run, nodeId }: { run: WorkflowRun; nodeId: string }) {
	const { t } = useTranslation();
	const [note, setNote] = React.useState('');
	// The verdict this card already sent — keeps the card answered (and
	// its buttons gone) for the roundtrip until the refetch drops it.
	const [settled, setSettled] = React.useState<{
		verdict: ApproveVerdict;
		note: string;
	} | null>(null);
	const approve = useApproveRun();

	const preview = previewText(run.data);

	const handleVerdict = async (verdict: ApproveVerdict) => {
		const sent = note.trim();
		try {
			await approve.mutateAsync({
				runId: run.id,
				body: { node_id: nodeId, verdict, note: sent || undefined },
			});
			setSettled({ verdict, note: sent });
			setNote('');
			toast.success(
				verdict === 'approve'
					? t('workflow.approvals.approved')
					: t('workflow.approvals.rejected'),
			);
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	return (
		<Item
			variant="outline"
			className={cn(
				'flex-col items-stretch gap-2.5 px-4 py-3',
				settled && 'opacity-60',
			)}
		>
			<div className="flex items-center justify-between gap-2">
				<span className="min-w-0 truncate font-mono text-[13px] font-medium">
					{nodeId}
				</span>
				<div className="flex shrink-0 items-center gap-2">
					<RunStatusBadge status="WAITING_HUMAN" />
					<span className="font-mono text-[11px] text-muted-foreground">
						{format(new Date(run.updated_at), 'yyyy-MM-dd HH:mm')}
					</span>
				</div>
			</div>
			<div className="text-xs text-muted-foreground">
				{t('workflow.approvals.workflowLabel')}:{' '}
				<Link
					to={`/workflow/runs/${run.id}`}
					className="text-foreground hover:underline"
				>
					{run.data.workflow_name}
				</Link>
			</div>
			<p className="line-clamp-3 rounded-md bg-muted/60 px-3 py-2 text-xs leading-relaxed text-text-secondary">
				{preview ?? (
					<span className="italic">{t('workflow.approvals.noPreview')}</span>
				)}
			</p>
			{settled ? (
				<div className="flex items-center gap-2 text-xs text-muted-foreground">
					{settled.verdict === 'approve' ? (
						<Check className="size-3.5 text-green-600 dark:text-green-400" />
					) : (
						<X className="size-3.5 text-destructive" />
					)}
					<span>
						{settled.verdict === 'approve'
							? t('workflow.approvals.approvedByYou')
							: t('workflow.approvals.rejectedByYou')}
						{settled.note ? ` — “${settled.note}”` : ''}
					</span>
				</div>
			) : (
				<div className="flex flex-wrap items-center gap-2">
					<Input
						value={note}
						placeholder={t('workflow.approvals.notePlaceholder')}
						aria-label={t('workflow.approvals.noteLabel', { id: nodeId })}
						className="h-8 min-w-40 flex-1 text-xs sm:max-w-72"
						onChange={(e) => setNote(e.target.value)}
					/>
					<Button
						variant="outline"
						size="sm"
						disabled={approve.isPending}
						className="border-green-600/50 text-green-700 hover:bg-green-600/10 dark:text-green-400"
						onClick={() => void handleVerdict('approve')}
					>
						{approve.isPending ? <Spinner className="size-3.5" /> : <Check />}
						{t('workflow.approvals.approve')}
					</Button>
					<Button
						variant="outline"
						size="sm"
						disabled={approve.isPending}
						className="border-destructive/50 text-destructive hover:bg-destructive/10"
						onClick={() => void handleVerdict('reject')}
					>
						{approve.isPending ? <Spinner className="size-3.5" /> : <X />}
						{t('workflow.approvals.reject')}
					</Button>
				</div>
			)}
		</Item>
	);
}

/**
 * The approvals inbox: every run parked at a `human_approval` node,
 * polled every 15s. The backend has no stored question — a card's
 * identity is the parked node id plus the workflow name — and each
 * parked node gets its own card so parallel approvals are all
 * answerable here.
 */
export default function WorkflowApprovalsPage() {
	const { t } = useTranslation();
	const queryClient = useQueryClient();
	const { runs, loading, error } = useWorkflowRuns(
		{ status: 'WAITING_HUMAN' },
		{ refetchInterval: 15000 },
	);

	// One card per parked node (a run can wait on several), not per run.
	const cards = runs.flatMap((run) =>
		run.data.status === 'WAITING_HUMAN'
			? run.data.graph.nodes
					.filter(
						(node) => run.data.node_states[node.id]?.status === 'WAITING_HUMAN',
					)
					.map((node) => ({ run, nodeId: node.id }))
			: [],
	);

	const handleRetry = () =>
		queryClient.invalidateQueries({ queryKey: WORKFLOW_RUNS_KEY });

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div>
						<div className="text-2xl font-semibold">{t('workflow.approvals.title')}</div>
						<div className="mt-1 text-sm text-muted-foreground">
							{t('workflow.approvals.subtitle')}
						</div>
					</div>
					<Badge variant="secondary" className="mt-1 shrink-0">
						{t('workflow.approvals.count', { count: cards.length })}
					</Badge>
				</div>
				<Separator />

				<div className="flex-1 overflow-y-auto px-6 py-6">
					{loading ? (
						<div className="space-y-3">
							{Array.from({ length: 3 }).map((_, i) => (
								<Skeleton key={i} className="h-40 rounded-lg" />
							))}
						</div>
					) : error ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<History />
								</EmptyMedia>
								<EmptyTitle>{t('workflow.approvals.loadFailed')}</EmptyTitle>
								<EmptyDescription>{error.message}</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={handleRetry}>
									{t('workflow.approvals.retry')}
								</Button>
							</EmptyContent>
						</Empty>
					) : cards.length === 0 ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<CheckCircle2 />
								</EmptyMedia>
								<EmptyTitle>{t('workflow.approvals.emptyTitle')}</EmptyTitle>
								<EmptyDescription>
									{t('workflow.approvals.emptyDescription')}
								</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" asChild>
									<Link to="/workflow/runs">
										<History />
										{t('workflow.approvals.goRuns')}
									</Link>
								</Button>
							</EmptyContent>
						</Empty>
					) : (
						<div className="space-y-3">
							{cards.map(({ run, nodeId }) => (
								<ApprovalCard key={`${run.id}:${nodeId}`} run={run} nodeId={nodeId} />
							))}
						</div>
					)}
				</div>
			</main>
		</div>
	);
}
