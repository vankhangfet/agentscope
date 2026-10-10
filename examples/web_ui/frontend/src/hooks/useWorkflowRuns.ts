import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { approveRun, cancelRun, listRuns, retryRun, startRun } from '../api';
import type { ApproveRequest, ListRunsParams, StartRunRequest, WorkflowRun } from '../api';

/**
 * Cache key prefix for run lists; each list's filters form the rest of
 * its key, and invalidating the prefix refreshes every one of them.
 * The stream hook (Tasks 3-6) keeps the open run fresh through SSE
 * `state` frames, not through this key.
 */
export const WORKFLOW_RUNS_KEY = ['workflow-runs'];

/** Stable empty list, so `runs` keeps its identity between renders. */
const NO_RUNS: WorkflowRun[] = [];

/**
 * Manages workflow runs.
 *
 * @param filters - Optional `workflow_id` / `status` filters; they are
 *   part of the query key, so each filtered view caches separately.
 * @returns The runs (newest first) plus their count and `loading` /
 *   `error` flags.
 */
export function useWorkflowRuns(filters?: ListRunsParams) {
	const { data, isPending, error } = useQuery({
		queryKey: [...WORKFLOW_RUNS_KEY, filters ?? {}],
		queryFn: () => listRuns(filters),
	});

	return {
		runs: data?.runs ?? NO_RUNS,
		total: data?.total ?? 0,
		loading: isPending,
		error: error as Error | null,
	};
}

/** Starts a run of a workflow and refreshes the run lists. */
export function useStartRun() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: ({ workflowId, body }: { workflowId: string; body: StartRunRequest }) =>
			startRun(workflowId, body),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKFLOW_RUNS_KEY }),
	});
}

/**
 * Answers an approval node and refreshes the run lists.
 *
 * The list key covers the specific run too: like `useSchedules`, every
 * view of a run derives from a list entry rather than a per-item key,
 * so there is no separate detail entry to invalidate.
 */
export function useApproveRun() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: ({ runId, body }: { runId: string; body: ApproveRequest }) =>
			approveRun(runId, body),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKFLOW_RUNS_KEY }),
	});
}

/** Cancels a run and refreshes the run lists. */
export function useCancelRun() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (runId: string) => cancelRun(runId),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKFLOW_RUNS_KEY }),
	});
}

/** Resets a failed run's failed and cancelled nodes, re-dispatches, and refreshes the run lists. */
export function useRetryRun() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (runId: string) => retryRun(runId),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKFLOW_RUNS_KEY }),
	});
}
