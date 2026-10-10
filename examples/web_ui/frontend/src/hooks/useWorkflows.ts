import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { createWorkflow, deleteWorkflow, getWorkflow, listWorkflows, updateWorkflow } from '../api';
import type { Workflow, WorkflowData } from '../api';

/**
 * Cache key for the workflow list. Exported so any write outside these
 * hooks can invalidate it. The single-workflow query nests under this
 * prefix, so invalidating the list refreshes it too.
 */
export const WORKFLOWS_KEY = ['workflows'];

/** Stable empty list, so `workflows` keeps its identity between renders. */
const NO_WORKFLOWS: Workflow[] = [];

/**
 * Manages workflow definitions.
 *
 * The list is cached under `['workflows']` and refreshed by every
 * mutation below — the react-query counterpart of `useSchedules`.
 */
export function useWorkflows() {
	const { data, isPending, error } = useQuery({
		queryKey: WORKFLOWS_KEY,
		queryFn: async () => (await listWorkflows()).workflows,
	});

	return {
		workflows: data ?? NO_WORKFLOWS,
		loading: isPending,
		error: error as Error | null,
	};
}

/**
 * One workflow's content, keyed under the list's prefix so every
 * workflow mutation — which invalidates `['workflows']` — refreshes
 * it along with the lists.
 *
 * @param workflowId - The workflow to load. Pass null to skip fetching.
 * @returns The workflow's `data` (id stays the caller's), plus
 *   `loading` / `error` flags.
 */
export function useWorkflow(workflowId: string | null) {
	const { data, isPending, error } = useQuery({
		queryKey: [...WORKFLOWS_KEY, workflowId],
		queryFn: () => getWorkflow(workflowId as string),
		enabled: workflowId !== null,
	});

	return {
		workflow: data?.data,
		loading: workflowId !== null && isPending,
		error: error as Error | null,
	};
}

/** Creates a workflow and refreshes the list. */
export function useCreateWorkflow() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (data: WorkflowData) => createWorkflow(data),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKFLOWS_KEY }),
	});
}

/** Replaces a workflow's content (bumping its version) and refreshes the list. */
export function useUpdateWorkflow() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: ({ workflowId, data }: { workflowId: string; data: WorkflowData }) =>
			updateWorkflow(workflowId, data),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKFLOWS_KEY }),
	});
}

/**
 * Deletes a workflow and refreshes the list. Runs outlive their
 * workflow — each holds a graph snapshot — so the run lists stay
 * valid and are not invalidated.
 */
export function useDeleteWorkflow() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (workflowId: string) => deleteWorkflow(workflowId),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: WORKFLOWS_KEY }),
	});
}
