import { useQueryClient } from '@tanstack/react-query';
import { format } from 'date-fns';
import { History, Play, X } from 'lucide-react';
import * as React from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { RUN_TERMINAL_STATUSES } from './components/runReadiness';
import { RunStatusBadge } from './components/RunStatusBadge';
import type { ListRunsParams, RunStatus, WorkflowRun } from '@/api';
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
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '@/components/ui/table';
import { useWorkflowRuns, WORKFLOW_RUNS_KEY } from '@/hooks/useWorkflowRuns';
import { useWorkflow } from '@/hooks/useWorkflows';
import { useTranslation } from '@/i18n/useI18n';

/** The filter's "no filter" value — radix Select rejects empty strings. */
const ALL_STATUSES = 'ALL';

/** Every run status, in filter order. */
const STATUSES: RunStatus[] = [
	'PENDING',
	'RUNNING',
	'WAITING_HUMAN',
	'SUCCEEDED',
	'FAILED',
	'CANCELLED',
];

/** One run row: workflow, status chip, trigger, timestamps. Anywhere
 * on the row opens the live detail view. */
function RunRow({ run, onOpen }: { run: WorkflowRun; onOpen: () => void }) {
	const { data } = run;
	return (
		<TableRow className="cursor-pointer" onClick={onOpen}>
			<TableCell className="max-w-[280px] px-4 py-2.5">
				<Link
					to={`/workflow/runs/${run.id}`}
					className="block truncate text-[12.5px] font-medium text-foreground hover:underline"
					title={data.workflow_name}
					// The row click already routes; keep the link from
					// double-firing the same navigation.
					onClick={(e) => e.stopPropagation()}
				>
					{data.workflow_name}
				</Link>
			</TableCell>
			<TableCell className="px-4 py-2.5">
				<RunStatusBadge status={data.status} />
			</TableCell>
			<TableCell className="px-4 py-2.5 font-mono text-[11px] text-text-secondary">
				{data.trigger}
			</TableCell>
			<TableCell className="px-4 py-2.5 font-mono text-[11px] text-text-secondary">
				{format(new Date(run.created_at), 'yyyy-MM-dd HH:mm')}
			</TableCell>
			<TableCell className="px-4 py-2.5 font-mono text-[11px] text-text-secondary">
				{data.finished_at
					? format(new Date(data.finished_at), 'yyyy-MM-dd HH:mm')
					: '—'}
			</TableCell>
		</TableRow>
	);
}

/**
 * The runs list: every run (optionally one workflow's, optionally one
 * status), newest first. Polls every 5s while any visible run is still
 * live; rows open the live detail view.
 */
export function WorkflowRunsPage() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const [searchParams, setSearchParams] = useSearchParams();

	const workflowId = searchParams.get('workflowId');
	const status = (searchParams.get('status') as RunStatus | null) ?? undefined;

	const filters: ListRunsParams = {
		...(workflowId != null && { workflowId }),
		...(status != null && { status }),
	};

	// Poll only while something visible is live: the effect flips the
	// flag on every fetch, and the final fetch that sees all-terminal
	// rows is itself the last poll.
	const [poll, setPoll] = React.useState(false);
	const { runs, total, loading, error } = useWorkflowRuns(filters, {
		refetchInterval: poll ? 5000 : false,
	});
	React.useEffect(() => {
		setPoll(runs.some((run) => !RUN_TERMINAL_STATUSES.includes(run.data.status)));
	}, [runs]);

	// The chip shows the workflow's current name; runs carry only a
	// start-time snapshot.
	const { workflow } = useWorkflow(workflowId);

	const setStatusFilter = (next: string) => {
		setSearchParams((params) => {
			if (next === ALL_STATUSES) params.delete('status');
			else params.set('status', next);
			return params;
		});
	};

	const clearWorkflowFilter = () => {
		setSearchParams((params) => {
			params.delete('workflowId');
			return params;
		});
	};

	const handleRetry = () => queryClient.invalidateQueries({ queryKey: WORKFLOW_RUNS_KEY });

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div>
						<div className="text-2xl font-semibold">{t('workflow.runs.title')}</div>
						<div className="mt-1 text-sm text-muted-foreground">
							{t('workflow.runs.subtitle')}
						</div>
					</div>
					<div className="flex shrink-0 items-center gap-2">
						{workflowId != null && (
							<Badge variant="secondary" className="gap-1 pr-1">
								<span className="max-w-48 truncate">
									{workflow?.name ?? workflowId}
								</span>
								<button
									type="button"
									className="flex size-4 items-center justify-center rounded-full hover:bg-muted-foreground/20"
									aria-label={t('workflow.runs.clearFilter')}
									onClick={clearWorkflowFilter}
								>
									<X className="size-3" />
								</button>
							</Badge>
						)}
						<Select value={status ?? ALL_STATUSES} onValueChange={setStatusFilter}>
							<SelectTrigger className="h-8 w-44">
								<SelectValue />
							</SelectTrigger>
							<SelectContent>
								<SelectItem value={ALL_STATUSES}>
									{t('workflow.runs.allStatuses')}
								</SelectItem>
								{STATUSES.map((value) => (
									<SelectItem key={value} value={value}>
										{t(`workflow.status.${value}`)}
									</SelectItem>
								))}
							</SelectContent>
						</Select>
					</div>
				</div>
				<Separator />

				<div className="flex-1 overflow-y-auto px-6 py-6">
					{loading ? (
						<div className="space-y-3">
							{Array.from({ length: 5 }).map((_, i) => (
								<Skeleton key={i} className="h-12 rounded-lg" />
							))}
						</div>
					) : error ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<History />
								</EmptyMedia>
								<EmptyTitle>{t('workflow.runs.loadFailed')}</EmptyTitle>
								<EmptyDescription>{error.message}</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={handleRetry}>
									{t('workflow.runs.retry')}
								</Button>
							</EmptyContent>
						</Empty>
					) : runs.length === 0 ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<History />
								</EmptyMedia>
								<EmptyTitle>{t('workflow.runs.emptyTitle')}</EmptyTitle>
								<EmptyDescription>
									{t('workflow.runs.emptyDescription')}
								</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" asChild>
									<Link to="/workflow">
										<Play />
										{t('workflow.runs.goWorkflows')}
									</Link>
								</Button>
							</EmptyContent>
						</Empty>
					) : (
						<section>
							<div className="mb-4 flex items-center gap-2">
								<span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
									{t('workflow.runs.label')}
								</span>
								<span className="rounded bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
									{t('workflow.runs.count', { count: total })}
								</span>
							</div>
							<div className="overflow-x-auto rounded-[16px] border border-border">
								<Table className="min-w-[640px]">
									<TableHeader>
										<TableRow className="border-border hover:bg-transparent">
											<TableHead className="px-4">
												{t('workflow.runs.table.workflow')}
											</TableHead>
											<TableHead className="px-4">
												{t('workflow.runs.table.status')}
											</TableHead>
											<TableHead className="px-4">
												{t('workflow.runs.table.trigger')}
											</TableHead>
											<TableHead className="px-4">
												{t('workflow.runs.table.created')}
											</TableHead>
											<TableHead className="px-4">
												{t('workflow.runs.table.finished')}
											</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{runs.map((run) => (
											<RunRow
												key={run.id}
												run={run}
												onOpen={() => navigate(`/workflow/runs/${run.id}`)}
											/>
										))}
									</TableBody>
								</Table>
							</div>
						</section>
					)}
				</div>
			</main>
		</div>
	);
}
