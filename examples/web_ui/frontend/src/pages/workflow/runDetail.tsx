import '@xyflow/react/dist/style.css';

import { useQuery } from '@tanstack/react-query';
import { format } from 'date-fns';
import { ArrowLeft, Ban, Check, History, RotateCcw, X } from 'lucide-react';
import * as React from 'react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { NodeOutputPanel } from './components/NodeOutputPanel';
import { RunGraphView } from './components/RunGraphView';
import { parkedNodes, RUN_TERMINAL_STATUSES } from './components/runReadiness';
import { RunStatusBadge } from './components/RunStatusBadge';
import type {
	ApproveVerdict,
	RunDetailResponse,
	RunStatus,
	RunStreamHandlers,
	WorkflowRunData,
} from '@/api';
import { getRun, openRunStream } from '@/api';
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
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { useAgents } from '@/hooks/useAgents';
import { useChannels } from '@/hooks/useChannels';
import { useApproveRun, useCancelRun, useRetryRun } from '@/hooks/useWorkflowRuns';
import { useTranslation } from '@/i18n/useI18n';

/** Cache key for the polling fallback's `getRun` snapshot. */
const RUN_KEY = (runId: string) => ['workflow-run', runId];

/**
 * The live run view: header (status, actions, approval prompt) over a
 * read-only graph and the selected node's output.
 *
 * Liveness: `openRunStream` paints the initial `state` frame, then
 * `node` / `run` frames patch that state in place. The server closes
 * the stream when the run goes terminal; anything else (drop, or a
 * retry reviving a finished run) reconnects with 1s→2s→5s backoff,
 * snapshotting via `getRun` on every reopen to fill the gap. A stream
 * that never opens falls back to plain polling (`mode`).
 */
export function WorkflowRunDetailPage() {
	const { t } = useTranslation();
	const { runId = '' } = useParams<{ runId: string }>();
	const { agents } = useAgents();
	const { channels } = useChannels();

	const [data, setData] = React.useState<WorkflowRunData | null>(null);
	const [mode, setMode] = React.useState<'stream' | 'polling'>('stream');
	// Bumped after a retry: the FAILED run's stream is already closed,
	// so the effect must reopen to follow the fresh dispatch.
	const [streamEpoch, setStreamEpoch] = React.useState(0);
	const [selectedNodeId, setSelectedNodeId] = React.useState<string | null>(null);
	const [note, setNote] = React.useState('');
	const [confirmCancel, setConfirmCancel] = React.useState(false);

	// The stream's close handler must know whether the run is terminal,
	// but the handlers close over no state — a ref carries the latest
	// run status without re-subscribing on every patch.
	const statusRef = React.useRef<RunStatus>('PENDING');
	// Which run the view last reset for: switching runs clears the
	// view, while a stream reopen (retry) keeps the current content.
	const runIdRef = React.useRef<string | null>(null);

	React.useEffect(() => {
		if (runIdRef.current !== runId) {
			runIdRef.current = runId;
			setData(null);
			setSelectedNodeId(null);
			setNote('');
		}
		// PENDING is non-terminal, so a close before the first state
		// frame still reconnects — exactly what a retried run needs.
		statusRef.current = 'PENDING';
		setMode('stream');

		const controller = new AbortController();
		let disposed = false;
		let disposer: (() => void) | null = null;
		let reconnectTimer: number | undefined;
		let attempt = 0;

		// Snapshot refetch: fills any gap a dropped stream left, and
		// lands the authoritative finished_at once the run is terminal.
		const refetch = () => {
			getRun(runId)
				.then((res) => {
					if (!disposed) {
						statusRef.current = res.data.status;
						setData(res.data);
					}
				})
				.catch(() => {
					// Best-effort gap fill; the reopen retries it.
				});
		};

		const connect = async (): Promise<void> => {
			try {
				const dispose = await openRunStream(runId, handlers, controller.signal);
				if (disposed) {
					dispose();
					return;
				}
				disposer = dispose;
				attempt = 0;
			} catch {
				// The stream could not open at all (server down, auth):
				// poll `getRun` instead of retrying forever.
				if (disposed || controller.signal.aborted) return;
				setMode('polling');
			}
		};

		const handlers: RunStreamHandlers = {
			onState: (state) => {
				if (disposed) return;
				statusRef.current = state.status;
				setData(state);
			},
			onNode: (event) => {
				if (disposed) return;
				setData((prev) => {
					if (!prev) return prev;
					const previous = prev.node_states[event.node_id];
					return {
						...prev,
						node_states: {
							...prev.node_states,
							[event.node_id]: {
								status: event.status,
								attempts: previous?.attempts ?? 0,
								output: previous?.output ?? null,
								error: event.error,
							},
						},
					};
				});
			},
			onRun: (event) => {
				if (disposed) return;
				statusRef.current = event.status;
				setData((prev) =>
					prev
						? { ...prev, status: event.status, error: event.error ?? null }
						: prev,
				);
				if (RUN_TERMINAL_STATUSES.includes(event.status)) refetch();
			},
			onClose: () => {
				if (disposed || controller.signal.aborted) return;
				// The server ends terminal runs itself; a close on a
				// live run is a drop (or a just-retried run) — snapshot
				// to fill the gap, then reopen with backoff, 5s cap.
				if (RUN_TERMINAL_STATUSES.includes(statusRef.current)) return;
				refetch();
				const delay = Math.min(1000 * 2 ** attempt, 5000);
				attempt += 1;
				reconnectTimer = window.setTimeout(() => {
					if (!disposed) void connect();
				}, delay);
			},
		};

		void connect();

		return () => {
			disposed = true;
			controller.abort();
			window.clearTimeout(reconnectTimer);
			disposer?.();
		};
	}, [runId, streamEpoch]);

	// Polling fallback: a local getRun query at 3s while live, stopping
	// once the snapshot reports a terminal status.
	const poll = useQuery({
		queryKey: RUN_KEY(runId),
		queryFn: () => getRun(runId),
		enabled: mode === 'polling',
		refetchInterval: (query) =>
			RUN_TERMINAL_STATUSES.includes(query.state.data?.data.status ?? 'PENDING')
				? false
				: 3000,
	});
	React.useEffect(() => {
		if (mode !== 'polling' || !poll.data) return;
		statusRef.current = poll.data.data.status;
		setData(poll.data.data);
	}, [mode, poll.data]);

	const approve = useApproveRun();
	const cancel = useCancelRun();
	const retry = useRetryRun();

	/** Adopt a mutation's response (approve/cancel/retry all return the
	 * fresh run content) as the view state. */
	const apply = (res: RunDetailResponse) => {
		statusRef.current = res.data.status;
		setData(res.data);
	};

	// The first parked node (graph order) drives the header's approve
	// block — one prompt even when several nodes wait; parallel approvals
	// are answered individually on the approvals page.
	const waiting = data ? (parkedNodes(data)[0] ?? null) : null;

	const selectedNode = data
		? (data.graph.nodes.find((node) => node.id === selectedNodeId) ?? null)
		: null;
	const selectedState = (selectedNodeId && data?.node_states[selectedNodeId]) || null;
	const selectedOutput =
		selectedNodeId != null
			? (data?.context[selectedNodeId] ?? selectedState?.output ?? null)
			: null;

	const handleVerdict = async (verdict: ApproveVerdict) => {
		if (!waiting) return;
		try {
			const res = await approve.mutateAsync({
				runId,
				body: {
					node_id: waiting.nodeId,
					verdict,
					note: note.trim() || undefined,
				},
			});
			setNote('');
			apply(res);
			toast.success(
				verdict === 'approve'
					? t('workflow.runDetail.approved')
					: t('workflow.runDetail.rejected'),
			);
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	const handleRetry = async () => {
		try {
			const res = await retry.mutateAsync(runId);
			apply(res);
			// The old stream closed when the run FAILED; reopen for the
			// fresh dispatch.
			setStreamEpoch((epoch) => epoch + 1);
			toast.success(t('workflow.runDetail.restarted'));
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	// ─── Loading / error shell ───────────────────────────────────────────

	if (!data) {
		const loadError = mode === 'polling' ? (poll.error as Error | null) : null;
		return (
			<div className="flex size-full p-2">
				<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
					<header className="flex items-center gap-2 border-b px-4 py-3">
						<Button variant="ghost" size="icon-sm" asChild>
							<Link to="/workflow/runs" aria-label={t('workflow.runDetail.back')}>
								<ArrowLeft />
							</Link>
						</Button>
						<span className="font-mono text-sm text-muted-foreground">{runId}</span>
					</header>
					<div className="flex min-h-0 flex-1 items-center justify-center">
						{loadError ? (
							<Empty className="border-none py-10">
								<EmptyHeader>
									<EmptyMedia variant="icon">
										<History />
									</EmptyMedia>
									<EmptyTitle>{t('workflow.runDetail.loadFailed')}</EmptyTitle>
									<EmptyDescription>{loadError.message}</EmptyDescription>
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

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<header className="border-b px-4 py-3">
					<div className="flex items-center gap-2">
						<Button variant="ghost" size="icon-sm" asChild>
							<Link to="/workflow/runs" aria-label={t('workflow.runDetail.back')}>
								<ArrowLeft />
							</Link>
						</Button>
						<div className="min-w-0 flex-1 truncate text-sm font-semibold">
							{data.workflow_name}
						</div>
						<Badge variant="secondary">
							{t('workflow.list.version', { version: data.workflow_version })}
						</Badge>
						<RunStatusBadge status={data.status} />
						{(data.status === 'RUNNING' || data.status === 'WAITING_HUMAN') && (
							<Button variant="outline" size="sm" onClick={() => setConfirmCancel(true)}>
								<Ban />
								{t('workflow.runDetail.cancel')}
							</Button>
						)}
						{data.status === 'FAILED' && (
							<Button
								variant="outline"
								size="sm"
								disabled={retry.isPending}
								onClick={() => void handleRetry()}
							>
								{retry.isPending ? <Spinner className="size-3.5" /> : <RotateCcw />}
								{t('workflow.runDetail.retry')}
							</Button>
						)}
					</div>
					<div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
						<span className="font-mono">{runId}</span>
						<span>
							{t('workflow.runDetail.trigger')}: {data.trigger}
						</span>
						<span>
							{t('workflow.runDetail.finished')}:{' '}
							{data.finished_at
								? format(new Date(data.finished_at), 'yyyy-MM-dd HH:mm:ss')
								: '—'}
						</span>
					</div>
					{data.status === 'FAILED' && data.error && (
						<p className="mt-2 rounded bg-destructive/10 p-2 font-mono text-[11px] leading-relaxed break-words text-destructive">
							{data.error}
						</p>
					)}
				</header>

				{waiting && (
					<section className="border-b bg-amber-500/5 px-4 py-3">
						<div className="flex flex-wrap items-center justify-between gap-2">
							<span className="text-xs font-semibold text-amber-600 dark:text-amber-400">
								{t('workflow.runDetail.waitingTitle')}
							</span>
							<div className="flex items-center gap-2">
								<Input
									value={note}
									placeholder={t('workflow.runDetail.notePlaceholder')}
									className="h-7 w-56 text-xs"
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
									{t('workflow.runDetail.approve')}
								</Button>
								<Button
									variant="outline"
									size="sm"
									disabled={approve.isPending}
									className="border-destructive/50 text-destructive hover:bg-destructive/10"
									onClick={() => void handleVerdict('reject')}
								>
									{approve.isPending ? <Spinner className="size-3.5" /> : <X />}
									{t('workflow.runDetail.reject')}
								</Button>
							</div>
						</div>
						<p className="mt-1 text-xs text-muted-foreground">
							{t('workflow.runDetail.waitingNode', { id: waiting.nodeId })}
						</p>
					</section>
				)}

				<div className="flex min-h-0 flex-1">
					<div className="relative min-w-0 flex-1">
						<RunGraphView
							graph={data.graph}
							nodeStates={data.node_states}
							agents={agents}
							channels={channels}
							selectedNodeId={selectedNodeId}
							onSelectNode={setSelectedNodeId}
						/>
					</div>
					<NodeOutputPanel
						node={selectedNode}
						state={selectedState}
						output={selectedOutput}
						onClose={() => setSelectedNodeId(null)}
					/>
				</div>
			</main>

			<DeleteDialog
				open={confirmCancel}
				onOpenChange={setConfirmCancel}
				title={t('workflow.runDetail.cancelTitle')}
				description={t('workflow.runDetail.cancelDescription')}
				confirmLabel={t('workflow.runDetail.cancelConfirm')}
				onConfirm={async () => {
					try {
						apply(await cancel.mutateAsync(runId));
						toast.success(t('workflow.runDetail.cancelled'));
					} catch {
						// The API client already toasted the failure detail.
					}
				}}
			/>
		</div>
	);
}
