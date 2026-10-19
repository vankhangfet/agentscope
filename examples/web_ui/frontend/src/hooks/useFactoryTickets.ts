import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
	approveFactoryTicket,
	cancelFactoryTicket,
	createFactoryTicket,
	getFactoryRoles,
	getFactoryTicket,
	listFactoryTickets,
	rejectFactoryTicket,
} from '../api';
import type {
	CreateFactoryTicketRequest,
	FactoryRoleGroupView,
	FactoryTicketView,
	ListFactoryTicketsParams,
} from '../api';

/**
 * Cache key prefix for factory ticket views; each list's filters and
 * each detail form the rest of its key, and invalidating the prefix
 * refreshes every one of them. The react-query counterpart of
 * `DISCUSSIONS_KEY`.
 */
export const FACTORY_TICKETS_KEY = ['factory-tickets'];

/** Cache key for the role roster — a catalog, not per-ticket state. */
export const FACTORY_ROSTER_KEY = ['factory-roster'];

/** Stable empty list, so `tickets` keeps its identity between renders. */
const NO_TICKETS: FactoryTicketView[] = [];

/** Stable empty list, so `groups` keeps its identity between renders. */
const NO_GROUPS: FactoryRoleGroupView[] = [];

/**
 * Manages the factory ticket list.
 *
 * @param filters - Optional `status` filter; part of the query key, so
 *   each filtered view caches separately.
 * @param options - Optional `refetchInterval` (react-query's), for
 *   views that poll while any visible ticket is still live.
 * @returns The tickets (newest first) plus their count and `loading` /
 *   `error` flags.
 */
export function useFactoryTickets(
	filters?: ListFactoryTicketsParams,
	options?: { refetchInterval?: number | false },
) {
	const { data, isPending, error } = useQuery({
		queryKey: [...FACTORY_TICKETS_KEY, filters ?? {}],
		queryFn: () => listFactoryTickets(filters),
		refetchInterval: options?.refetchInterval,
	});

	return {
		tickets: data?.tickets ?? NO_TICKETS,
		total: data?.total ?? 0,
		loading: isPending,
		error: error as Error | null,
	};
}

/**
 * One ticket's content, keyed under the list's prefix so every ticket
 * mutation — which invalidates `['factory-tickets']` — refreshes it
 * along with the lists. The `useDiscussion` pattern.
 *
 * @param ticketId - The ticket to load. Pass null to skip fetching.
 * @param options - Optional `refetchInterval` (react-query's), for the
 *   detail view's live polling.
 * @returns The ticket's view plus `loading` / `error` flags.
 */
export function useFactoryTicket(
	ticketId: string | null,
	options?: { refetchInterval?: number | false },
) {
	const { data, isPending, error } = useQuery({
		queryKey: [...FACTORY_TICKETS_KEY, 'detail', ticketId],
		queryFn: () => getFactoryTicket(ticketId as string),
		enabled: ticketId !== null,
		refetchInterval: options?.refetchInterval,
	});

	return {
		ticket: data?.data,
		loading: ticketId !== null && isPending,
		error: error as Error | null,
	};
}

/** Opens a ticket and refreshes the factory views. */
export function useCreateFactoryTicket() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (body: CreateFactoryTicketRequest) => createFactoryTicket(body),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: FACTORY_TICKETS_KEY }),
	});
}

/**
 * The factory's role roster from `GET /factory/roles`.
 *
 * Compiled into the server, so the catalog cannot change while it is
 * up — hence `staleTime: Infinity`, the presets pattern: every
 * dialog and page that needs the team reads the one cached fetch.
 *
 * @returns The role groups (in phase order) plus the judge, and
 *   `loading` / `error` flags.
 */
export function useFactoryRoster() {
	const { data, isPending, error } = useQuery({
		queryKey: FACTORY_ROSTER_KEY,
		queryFn: getFactoryRoles,
		staleTime: Infinity,
	});

	return {
		groups: data?.groups ?? NO_GROUPS,
		judge: data?.judge ?? null,
		loading: isPending,
		error: error as Error | null,
	};
}

/**
 * Approves a gate and refreshes the factory views, so the detail page
 * repaints with the advanced phase on its own.
 */
export function useApproveFactoryTicket() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: ({ ticketId, note }: { ticketId: string; note: string }) =>
			approveFactoryTicket(ticketId, note),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: FACTORY_TICKETS_KEY }),
	});
}

/**
 * Rejects a gate — the current phase re-runs — and refreshes the
 * factory views.
 */
export function useRejectFactoryTicket() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: ({ ticketId, note }: { ticketId: string; note: string }) =>
			rejectFactoryTicket(ticketId, note),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: FACTORY_TICKETS_KEY }),
	});
}

/**
 * Cancels a ticket and refreshes the factory views. The prefix key
 * covers the detail view too, so the live page repaints on its own.
 */
export function useCancelFactoryTicket() {
	const queryClient = useQueryClient();
	return useMutation({
		mutationFn: (ticketId: string) => cancelFactoryTicket(ticketId),
		onSuccess: () => queryClient.invalidateQueries({ queryKey: FACTORY_TICKETS_KEY }),
	});
}
