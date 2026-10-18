import { client } from './client';
import type { ChatModelConfig } from './types';

/** Who approves the ticket's gates. Mirrors the backend's `FactoryMode`. */
export type FactoryMode = 'COLLABORATIVE' | 'DELEGATED';

/** The phases a ticket walks through, in order. Mirrors `FactoryPhase`. */
export type FactoryPhase = 'REQUIREMENTS' | 'ARCHITECTURE' | 'DEVELOP';

/** Where a ticket stands. Mirrors the backend's `FactoryStatus`. */
export type FactoryStatus =
	| 'PENDING'
	| 'RUNNING'
	| 'WAITING_HUMAN'
	| 'COMPLETED'
	| 'FAILED'
	| 'CANCELLED';

/** One development task's own lifecycle. Mirrors `FactoryTask.status`. */
export type FactoryTaskStatus = 'PENDING' | 'RUNNING' | 'DONE' | 'FAILED';

/** One task in the phase 3 breakdown, with its own lifecycle. */
export interface FactoryTask {
	/** What the task delivers, in one line. */
	title: string;
	/** The detail the Developer turn works from. */
	description: string;
	/** The logical role name the task is routed to. */
	assignee: string;
	status: FactoryTaskStatus;
	/** What the Developer turn produced, once it has. */
	output: string | null;
}

/** One entry in the ticket's decision log. */
export interface FactoryDecision {
	/** The phase the decision was taken in. */
	phase: FactoryPhase;
	/** The discussion that produced it, when one did. */
	discussion_id: string | null;
	/** The discussion's synthesis, or the gate's resolution. */
	conclusion: string;
	/** The human's gate note (approve or reject). */
	note: string;
	/** Whether the discussion converged. */
	consensus: boolean;
	created_at: string;
}

/**
 * One factory ticket, as the API shows it — the `data` projection plus
 * the record's own ids, the persisted fields verbatim.
 */
export interface FactoryTicketView {
	ticket_id: string;
	/** The human's problem, in one line. */
	title: string;
	/** The problem statement the factory works from. */
	description: string;
	mode: FactoryMode;
	phase: FactoryPhase;
	status: FactoryStatus;
	/** Every role's model (v1: one per ticket). */
	chat_model_config: ChatModelConfig;
	/** phase → discussion id: the discussion that ran (or, after a
	 * reject, the fresh re-run) for that phase. */
	discussions: Partial<Record<FactoryPhase, string>>;
	/** The requirements artifact, once phase 1 concludes. */
	requirements: string | null;
	/** The architecture artifact, once phase 2 concludes. */
	architecture: string | null;
	/** The reviewer's output, once the tasks are done. */
	review: string | null;
	/** The phase 3 task breakdown with each task's own lifecycle. */
	tasks: FactoryTask[];
	/** The decision log, in append order (newest last). */
	decisions: FactoryDecision[];
	/** The per-ticket workspace, minted when DEVELOP starts. */
	workspace_id: string | null;
	/** Why the ticket failed, when it did. */
	error: string | null;
	/** manual | api. */
	trigger: string;
	created_at: string;
	updated_at: string;
	/** Set once the ticket reaches a terminal state. */
	finished_at: string | null;
}

export interface CreateFactoryTicketRequest {
	/** The human's problem, in one line; non-empty. */
	title: string;
	/** The problem statement the factory works from. */
	description?: string;
	/** Every role's model (v1: one per ticket). */
	chat_model_config: ChatModelConfig;
	/** Who approves the gates (default COLLABORATIVE). */
	mode?: FactoryMode;
}

export interface CreateFactoryTicketResponse {
	ticket_id: string;
}

/** Query parameters accepted by `GET /factory/`. */
export interface ListFactoryTicketsParams {
	/** Only tickets in this status when given. */
	status?: FactoryStatus;
}

export interface ListFactoryTicketsResponse {
	/** The tickets, newest first. */
	tickets: FactoryTicketView[];
	total: number;
}

export interface FactoryTicketDetailResponse {
	data: FactoryTicketView;
}

/**
 * Factory statuses no further transition can follow — the client
 * mirror of the engine's terminal set. Views poll a ticket only while
 * it sits outside this list.
 */
export const FACTORY_TERMINAL_STATUSES: readonly FactoryStatus[] = [
	'COMPLETED',
	'FAILED',
	'CANCELLED',
];

// ─── Client calls ─────────────────────────────────────────────────────────────

/** Open a ticket: validates, persists it PENDING and dispatches it. */
export function createFactoryTicket(body: CreateFactoryTicketRequest) {
	return client.post<CreateFactoryTicketResponse>('/factory/', body);
}

/** List the caller's tickets (optionally one status), newest first. */
export function listFactoryTickets(params?: ListFactoryTicketsParams) {
	return client.get<ListFactoryTicketsResponse>('/factory/', {
		...(params?.status != null && { status: params.status }),
	});
}

/** Read one ticket's persisted state. */
export function getFactoryTicket(ticketId: string) {
	return client.get<FactoryTicketDetailResponse>(`/factory/${ticketId}`);
}

/**
 * Approve a gate: advance to the next phase, or complete the ticket
 * at the final gate. Only a WAITING_HUMAN ticket is answerable.
 */
export function approveFactoryTicket(ticketId: string, note = '') {
	return client.post<FactoryTicketDetailResponse>(`/factory/${ticketId}/approve`, {
		note,
	});
}

/**
 * Reject a gate: re-run the current phase from scratch — a fresh
 * discussion replaces the phase's recorded one.
 */
export function rejectFactoryTicket(ticketId: string, note = '') {
	return client.post<FactoryTicketDetailResponse>(`/factory/${ticketId}/reject`, {
		note,
	});
}

/** Cancel a ticket: stop its walker and cascade into its discussion. */
export function cancelFactoryTicket(ticketId: string) {
	return client.post<FactoryTicketDetailResponse>(
		`/factory/${ticketId}/cancel`,
		null,
	);
}
