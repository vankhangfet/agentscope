import { client } from './client';
import type {
	CreateDiscussionRequest,
	CreateDiscussionResponse,
	DiscussionDetailResponse,
	DiscussionStatus,
	ListDiscussionsParams,
	ListDiscussionsResponse,
} from './types';

/**
 * Discussion statuses no further transition can follow — the client
 * mirror of the engine's terminal set. Views poll a discussion only
 * while it sits outside this list.
 */
export const DISCUSSION_TERMINAL_STATUSES: readonly DiscussionStatus[] = [
	'COMPLETED',
	'FAILED',
	'CANCELLED',
];

// ─── Client calls ─────────────────────────────────────────────────────────────

/** Start a discussion: validates, persists it PENDING and dispatches it. */
export function createDiscussion(body: CreateDiscussionRequest) {
	return client.post<CreateDiscussionResponse>('/discussion/', body);
}

/** List the caller's discussions (optionally one status), newest first. */
export function listDiscussions(params?: ListDiscussionsParams) {
	return client.get<ListDiscussionsResponse>('/discussion/', {
		...(params?.status != null && { status: params.status }),
	});
}

/** Read one discussion's persisted state. */
export function getDiscussion(discussionId: string) {
	return client.get<DiscussionDetailResponse>(`/discussion/${discussionId}`);
}

/** Cancel a discussion: stop its loop and mark the record CANCELLED. */
export function cancelDiscussion(discussionId: string) {
	return client.post<DiscussionDetailResponse>(
		`/discussion/${discussionId}/cancel`,
		null,
	);
}

/**
 * Rerun a discussion: clone its configuration (topic, debaters, judge,
 * round ceiling) into a brand-new discussion and dispatch it. The
 * source is left untouched; the response carries the new id. To rerun
 * with changes, create a new discussion from an edited payload instead.
 */
export function rerunDiscussion(discussionId: string) {
	return client.post<CreateDiscussionResponse>(
		`/discussion/${discussionId}/rerun`,
		null,
	);
}

/**
 * Delete a discussion with everything its run created: the engine
 * cancels a still-live debate first, then removes the participant
 * agents (with their sessions and message logs) and the record
 * itself. Answers 204; 404 when the caller owns no such discussion.
 */
export function deleteDiscussion(discussionId: string) {
	return client.delete(`/discussion/${discussionId}`);
}
