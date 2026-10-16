import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { cancelDiscussion, createDiscussion, getDiscussion, listDiscussions } from '../api';
import type {
	CreateDiscussionRequest,
	Discussion,
	ListDiscussionsParams,
} from '../api';

/**
 * Cache key prefix for discussion views; each list's filters and each
 * detail form the rest of its key, and invalidating the prefix
 * refreshes every one of them. The react-query counterpart of
 * `WORKFLOW_RUNS_KEY`.
 */
export const DISCUSSIONS_KEY = ['discussions'];

/** Stable empty list, so `discussions` keeps its identity between renders. */
const NO_DISCUSSIONS: Discussion[] = [];

/**
 * Manages the discussion list.
 *
 * @param filters - Optional `status` filter; part of the query key, so
 *   each filtered view caches separately.
 * @param options - Optional `refetchInterval` (react-query's), for
 *   views that poll while any visible discussion is still live.
 * @returns The discussions (newest first) plus their count and
 *   `loading` / `error` flags.
 */
export function useDiscussions(
	filters?: ListDiscussionsParams,
	options?: { refetchInterval?: number | false },
) {
	const { data, isPending, error } = useQuery({
		queryKey: [...DISCUSSIONS_KEY, filters ?? {}],
		queryFn: () => listDiscussions(filters),
		refetchInterval: options?.refetchInterval,
	});

	return {
		discussions: data?.discussions ?? NO_DISCUSSIONS,
		total: data?.total ?? 0,
		loading: isPending,
		error: error as Error | null,
	};
}

/**
 * One discussion's content, keyed under the list's prefix so every
 * discussion mutation — which invalidates `['discussions']` — refreshes
 * it along with the lists. The `useWorkflow` pattern.
 *
 * @param discussionId - The discussion to load. Pass null to skip fetching.
 * @param options - Optional `refetchInterval` (react-query's), for the
 *   detail view's live polling.
 * @returns The discussion's `data` plus `loading` / `error` flags.
 */
export function useDiscussion(
	discussionId: string | null,
	options?: { refetchInterval?: number | false },
) {
	const { data, isPending, error } = useQuery({
		queryKey: [...DISCUSSIONS_KEY, 'detail', discussionId],
		queryFn: () => getDiscussion(discussionId as string),
		enabled: discussionId !== null,
		refetchInterval: options?.refetchInterval,
	});

	return {
		discussion: data?.data,
		loading: discussionId !== null && isPending,
		error: error as Error | null,
	};
}

/** Starts a discussion and refreshes the discussion views. */
export function useCreateDiscussion() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (body: CreateDiscussionRequest) => createDiscussion(body),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: DISCUSSIONS_KEY }),
	});
}

/**
 * Cancels a discussion and refreshes the discussion views. The prefix
 * key covers the detail view too, so the live page repaints on its own.
 */
export function useCancelDiscussion() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (discussionId: string) => cancelDiscussion(discussionId),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: DISCUSSIONS_KEY }),
	});
}
