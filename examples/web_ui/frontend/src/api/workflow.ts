import { client } from './client';
import type { ChatModelConfig, RecordBase } from './types';

// ─── Graph ────────────────────────────────────────────────────────────────────

/** Where a node sits on the builder canvas. */
export interface WorkflowPosition {
	x: number;
	y: number;
}

/** Run one agent to completion. */
export interface AgentNodeConfig {
	type: 'agent';
	/** The agent this node runs. */
	agent_id: string;
	/**
	 * Mustache-style template over the run blackboard, e.g.
	 * `{{nodes.researcher.output.text}}` or `{{input.topic}}`.
	 */
	prompt_template?: string;
	/** Retries before the node fails the run (0-10). */
	max_retries?: number;
}

/** One arm of a condition node. */
export interface ConditionBranch {
	/** Matches an outgoing edge's `branch`. */
	label: string;
	/**
	 * Either an expression over the blackboard (e.g.
	 * `nodes.check.output.structured.passed == true`) or
	 * `llm_judge: <instruction>` for a model verdict.
	 */
	rule: string;
}

/** Pick one outgoing edge by rule or model verdict. */
export interface ConditionNodeConfig {
	type: 'condition';
	/** Candidate arms; exactly one is chosen per run. */
	branches: ConditionBranch[];
	/** Model used by `llm_judge` rules; null inherits the run default. */
	chat_model_config?: ChatModelConfig | null;
}

/** Iterate a list, or retry a body until a verifier passes. */
export interface LoopNodeConfig {
	type: 'loop';
	/**
	 * `iterate` fans the body out over a list; `retry_until` reruns the
	 * body until the verifier passes.
	 */
	mode: 'iterate' | 'retry_until';
	/** `iterate`: template resolving to a JSON list. */
	list_template?: string;
	/** `retry_until`: the agent rerun on each attempt. */
	body_agent_id?: string;
	/** How a list item is passed to the body agent. */
	item_template?: string;
	/** `retry_until`: what the verifier must accept. */
	verifier_prompt?: string;
	/** `retry_until`: attempt budget (1-20). */
	max_attempts?: number;
	/** `iterate`: run items concurrently (bounded). */
	parallel?: boolean;
}

/** Park the run until an approver answers. */
export interface HumanApprovalNodeConfig {
	type: 'human_approval';
	/** User ids asked to verdict; empty means the run owner. */
	approvers?: string[];
	/** How long the run may sit parked. */
	timeout_hours?: number;
	/**
	 * `fail` ends the run as FAILED on timeout; any other value must
	 * match an outgoing edge's `branch`.
	 */
	timeout_branch?: string;
}

/** Send one rendered message to a configured channel. */
export interface NotifyNodeConfig {
	type: 'notify';
	/** The channel this node notifies (an enabled channel record id). */
	channel_id: string;
	/**
	 * Mustache-style template over the run blackboard, e.g.
	 * `{{nodes.researcher.output.text}}`; the rendered text is sent to
	 * the channel. Telegram caps one message at 4096 characters.
	 */
	message_template: string;
	/** Retries before the node fails the run (0-10). */
	max_retries?: number;
}

/** A node's config, discriminated on `type`. */
export type NodeConfig =
	| AgentNodeConfig
	| ConditionNodeConfig
	| LoopNodeConfig
	| HumanApprovalNodeConfig
	| NotifyNodeConfig;

/** One step on the canvas. */
export interface WorkflowNode {
	/**
	 * Unique node id within the workflow (alphanumerics, dashes and
	 * underscores; no colons — the engine reserves those for derived
	 * session keys).
	 */
	id: string;
	/** Canvas coordinates (React Flow). */
	position?: WorkflowPosition;
	config: NodeConfig;
}

/** A directed connection between two nodes. */
export interface WorkflowEdge {
	/** Unique edge id within the workflow. */
	id: string;
	/** Outgoing node id. */
	source: string;
	/** Incoming node id. */
	target: string;
	/**
	 * Label selecting this edge from a condition or approval node;
	 * null on plain sequence edges.
	 */
	branch?: string | null;
}

/** The DAG itself, React Flow-compatible. */
export interface WorkflowGraph {
	nodes: WorkflowNode[];
	edges?: WorkflowEdge[];
}

// ─── Workflow ─────────────────────────────────────────────────────────────────

/** A workflow's editable content. */
export interface WorkflowData {
	/** Display name. */
	name: string;
	/** What it does. */
	description?: string;
	graph: WorkflowGraph;
	/** Bumped on each update. */
	version?: number;
}

/** A stored workflow, as `GET /workflow/` list entries return. */
export interface Workflow extends RecordBase {
	/** The owner's user id. */
	user_id: string;
	data: WorkflowData;
}

/** Response of `GET` / `PATCH /workflow/{id}`: the content, no record shell. */
export interface WorkflowDetailResponse {
	data: WorkflowData;
}

export interface CreateWorkflowRequest {
	data: WorkflowData;
}

export interface CreateWorkflowResponse {
	workflow_id: string;
}

export interface UpdateWorkflowRequest {
	data: WorkflowData;
}

export interface ListWorkflowsResponse {
	workflows: Workflow[];
	total: number;
}

// ─── Template catalog ─────────────────────────────────────────────────────────

/** One reference a template leaves empty and an install must map to a
 * real record before creating the workflow. */
export interface TemplateSlot {
	/** The node whose config carries the empty reference. */
	node_id: string;
	/** What the mapped record is for — the agent's job, or the
	 * channel's role in the flow. */
	label: string;
	/** Extra guidance shown under the label. */
	hint?: string;
}

/** One catalog entry from `GET /workflow/templates`. */
export interface WorkflowTemplateView {
	id: string;
	/** The name the created workflow starts from. */
	name: string;
	description: string;
	/** The gallery group it shows under (e.g. "Digest"). */
	category: string;
	/**
	 * The full graph with its slots empty: agent nodes' `agent_id` and
	 * notify nodes' `channel_id` are `""` and must be mapped before
	 * posting the graph to `POST /workflow/` — the save is strict.
	 */
	graph: WorkflowGraph;
	/** Every agent node in the graph, with what it is for. */
	agent_slots: TemplateSlot[];
	/** Every notify node in the graph, with what it is for. */
	channel_slots: TemplateSlot[];
}

export interface ListWorkflowTemplatesResponse {
	templates: WorkflowTemplateView[];
	total: number;
}

// ─── Run ──────────────────────────────────────────────────────────────────────

/** Where a workflow run stands. Mirrors the backend's `RunStatus` enum. */
export type RunStatus =
	| 'PENDING'
	| 'RUNNING'
	| 'WAITING_HUMAN'
	| 'SUCCEEDED'
	| 'FAILED'
	| 'CANCELLED';

/** One node's progress inside a run. */
export interface NodeRunState {
	/** The node's own lifecycle (PENDING..SUCCEEDED/FAILED/CANCELLED). */
	status: RunStatus;
	/** Dispatches so far. */
	attempts: number;
	/** What the node wrote to the blackboard, if it finished. */
	output?: Record<string, unknown> | null;
	/** The failure text when status is FAILED. */
	error?: string | null;
}

/** A run's persisted content. */
export interface WorkflowRunData {
	/** The workflow this run executes (id at start time). */
	workflow_id: string;
	/** The workflow version snapshot. */
	workflow_version: number;
	/** The workflow's display name snapshot. */
	workflow_name: string;
	/** The graph snapshot this run walks. */
	graph: WorkflowGraph;
	/** What the caller handed the run. */
	input: Record<string, unknown>;
	status: RunStatus;
	/** Run-level default model for agent nodes without one. */
	chat_model_config?: ChatModelConfig | null;
	/** manual | schedule | api | chat_tool. */
	trigger: string;
	/** Blackboard: node id → output dict. */
	context: Record<string, Record<string, unknown>>;
	/** Node id → progress. */
	node_states: Record<string, NodeRunState>;
	/** Why the whole run failed, when it did. */
	error?: string | null;
	/** Set once the run reaches a terminal state. */
	finished_at?: string | null;
}

/** A stored run, as `GET /workflow/runs` list entries return. */
export interface WorkflowRun extends RecordBase {
	/** The owner's user id. */
	user_id: string;
	data: WorkflowRunData;
}

/**
 * Response of `GET /workflow/runs/{id}` and the approve / cancel /
 * retry endpoints: the run's content, no record shell. The id is the
 * one the caller addressed the request with.
 */
export interface RunDetailResponse {
	data: WorkflowRunData;
}

export interface StartRunRequest {
	/** What the run's nodes template against. */
	input?: Record<string, unknown>;
	/** Run-level default model for modelless agent nodes. */
	chat_model_config?: ChatModelConfig | null;
}

export interface StartRunResponse {
	run_id: string;
}

/** Query parameters accepted by `GET /workflow/runs`. */
export interface ListRunsParams {
	/** Only this workflow's runs when given. */
	workflowId?: string;
	/** Only runs in this status when given. */
	status?: RunStatus;
}

export interface ListRunsResponse {
	/** The runs, newest first. */
	runs: WorkflowRun[];
	total: number;
}

/** An approver's answer to a `human_approval` node. */
export type ApproveVerdict = 'approve' | 'reject';

export interface ApproveRequest {
	/** The approval node being answered. */
	node_id: string;
	/** `approve` follows the approve edge; `reject` the other. */
	verdict: ApproveVerdict;
	/** Optional note recorded with the verdict. */
	note?: string;
}

// ─── Credential test ──────────────────────────────────────────────────────────

/** Request body for testing a credential with one model call. */
export interface TestCredentialRequest {
	/** The model name to call once. */
	model: string;
	/** Model parameters for the test call. */
	parameters?: Record<string, unknown>;
}

/**
 * Outcome of one credential test call. Provider, authentication and
 * network failures are data, not server errors: they come back as
 * `ok: false` with the error text in `detail`.
 */
export interface TestCredentialResult {
	ok: boolean;
	/** What happened, user-facing. */
	detail: string;
}

// ─── Event stream ─────────────────────────────────────────────────────────────

/** Fields every engine event on a run's channel carries. */
interface RunEventBase {
	run_id: string;
	/** The status the node or run moved to. */
	status: RunStatus;
	/** Failure text when the new status is FAILED; null otherwise. */
	error: string | null;
}

/** A node transition the engine published. */
export interface WorkflowNodeEvent extends RunEventBase {
	type: 'node';
	/** Which node moved. */
	node_id: string;
}

/** A whole-run transition the engine published. */
export interface WorkflowRunEvent extends RunEventBase {
	type: 'run';
	/** Always null: run events carry no node. */
	node_id: null;
}

/** Callbacks `openRunStream` dispatches by SSE event name. */
export interface RunStreamHandlers {
	/**
	 * The run's full state — the frame that opens the stream, in the
	 * same shape `getRun` returns (the `data` payload, no record shell).
	 */
	onState: (data: WorkflowRunData) => void;
	onNode?: (event: WorkflowNodeEvent) => void;
	onRun?: (event: WorkflowRunEvent) => void;
	/**
	 * Fires exactly once when the stream ends, however it ends: the
	 * server closed it (the run reached a terminal status), the
	 * connection dropped, or the disposer was called. Reconnecting is
	 * the consumer's call, not this helper's.
	 */
	onClose?: () => void;
}

// ─── Client calls ─────────────────────────────────────────────────────────────

export function listWorkflows() {
	return client.get<ListWorkflowsResponse>('/workflow/');
}

/**
 * The built-in template catalog. Static, auth-free server data (the
 * MCP-presets pattern), so callers cache it indefinitely.
 */
export function listWorkflowTemplates() {
	return client.get<ListWorkflowTemplatesResponse>('/workflow/templates');
}

export function getWorkflow(workflowId: string) {
	return client.get<WorkflowDetailResponse>(`/workflow/${workflowId}`);
}

export function createWorkflow(data: WorkflowData) {
	return client.post<CreateWorkflowResponse>('/workflow/', { data });
}

export function updateWorkflow(workflowId: string, data: WorkflowData) {
	return client.patch<WorkflowDetailResponse>(`/workflow/${workflowId}`, { data });
}

export function deleteWorkflow(workflowId: string) {
	return client.delete(`/workflow/${workflowId}`);
}

export function startRun(workflowId: string, body: StartRunRequest) {
	return client.post<StartRunResponse>(`/workflow/${workflowId}/run`, body);
}

export function listRuns(params?: ListRunsParams) {
	return client.get<ListRunsResponse>('/workflow/runs', {
		...(params?.workflowId != null && { workflow_id: params.workflowId }),
		...(params?.status != null && { status: params.status }),
	});
}

export function getRun(runId: string) {
	return client.get<RunDetailResponse>(`/workflow/runs/${runId}`);
}

export function approveRun(runId: string, body: ApproveRequest) {
	return client.post<RunDetailResponse>(`/workflow/runs/${runId}/approve`, body);
}

export function cancelRun(runId: string) {
	return client.post<RunDetailResponse>(`/workflow/runs/${runId}/cancel`, null);
}

export function retryRun(runId: string) {
	return client.post<RunDetailResponse>(`/workflow/runs/${runId}/retry`, null);
}

/**
 * Exercise one credential with a single minimal model call.
 *
 * Errors are silent at the client level: callers surface provider /
 * auth / network failures in their own UI (the test dialog's result
 * banner), and the client-wide toast would double-report them.
 */
export function testCredential(credentialId: string, body: TestCredentialRequest) {
	return client.post<TestCredentialResult>(
		`/credential/${credentialId}/test`,
		body,
		undefined,
		{ silent: true },
	);
}

/**
 * Subscribe to a run's live event stream via SSE.
 *
 * Opens a long-lived ``GET /workflow/runs/{run_id}/stream`` connection.
 * The server opens with one ``state`` frame (the full run content),
 * then forwards every engine transition as ``node`` / ``run`` frames,
 * and heartbeats with comment frames every 30s. The stream ends when
 * the run does; nothing here reconnects — a consumer that wants
 * reconnection (or wants to follow a retry's fresh dispatch) opens a
 * new stream in `onClose`.
 *
 * Uses fetch-based SSE (not native ``EventSource``) so the
 * ``X-User-ID`` custom header is sent — same reader as
 * `sessionApi.streamEvents`, extended to pair each frame's ``event:``
 * line with its ``data:`` line.
 *
 * @param runId - The run to subscribe to.
 * @param handlers - Dispatched by SSE event name; see {@link RunStreamHandlers}.
 * @param signal - Optional abort signal; aborting it ends the stream.
 * @returns A disposer that aborts the connection (and so fires
 *   `onClose`). The promise itself rejects only when the connection
 *   could not be opened — `client.stream`'s usual error handling
 *   (toast included) applies there.
 */
export async function openRunStream(
	runId: string,
	handlers: RunStreamHandlers,
	signal?: AbortSignal,
): Promise<() => void> {
	// Own controller so the returned disposer can tear the connection
	// down independently of the caller's signal; both feed the same abort.
	const controller = new AbortController();
	const forwardAbort = () => controller.abort();
	signal?.addEventListener('abort', forwardAbort);

	// If the connection never opens this rejects (with client.stream's
	// usual error handling, toast included) — the listener comes back
	// off and no disposer is handed out; onClose is a stream-lifetime
	// callback, and there was no stream.
	let res: Response;
	try {
		res = await client.stream(`/workflow/runs/${runId}/stream`, {
			method: 'GET',
			signal: controller.signal,
		});
	} catch (e) {
		signal?.removeEventListener('abort', forwardAbort);
		throw e;
	}

	const reader = res.body!.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	// SSE frame being assembled: the backend sends each event as an
	// `event:` line, a single `data:` line (its JSON never contains a
	// raw newline — they are escaped), and a blank separator line.
	let eventName = '';
	let data = '';

	const dispatch = () => {
		if (!data) return;
		switch (eventName) {
			case 'state':
				handlers.onState((JSON.parse(data) as RunDetailResponse).data);
				break;
			case 'node':
				handlers.onNode?.(JSON.parse(data) as WorkflowNodeEvent);
				break;
			case 'run':
				handlers.onRun?.(JSON.parse(data) as WorkflowRunEvent);
				break;
			default:
				// Unknown event names (and the default `message`) are
				// ignored — the stream is forwards-compatible by design.
				break;
		}
	};

	const pump = (async () => {
		try {
			while (true) {
				const { done, value } = await reader.read();
				if (done) break;

				buffer += decoder.decode(value, { stream: true });
				const lines = buffer.split('\n');
				buffer = lines.pop() ?? '';

				for (const line of lines) {
					if (line === '') {
						// Blank line: end of frame — dispatch and reset.
						dispatch();
						eventName = '';
						data = '';
					} else if (line.startsWith(':')) {
						// SSE comment frames are silently skipped
						// (used for heartbeats).
					} else if (line.startsWith('event: ')) {
						eventName = line.slice(7).trim();
					} else if (line.startsWith('data: ')) {
						data = line.slice(6).trim();
					}
				}
			}
		} catch {
			// reader.read() rejects when an abort lands mid-read, which
			// is the disposer's normal exit, and a handler that throws
			// ends its own stream — either way the stream is over and
			// onClose (in finally) is the report. Anything else is a
			// connection drop the consumer may reconnect from.
		} finally {
			reader.releaseLock();
			signal?.removeEventListener('abort', forwardAbort);
			handlers.onClose?.();
		}
	})();

	// The pump cannot reject (everything it awaits is inside the try),
	// and its completion is reported through onClose, not here.
	void pump;

	return () => controller.abort();
}

/** The workflow API in the house `xxxApi` object style, for consumers
 * that prefer it over the standalone functions above. */
export const workflowApi = {
	list: listWorkflows,
	listTemplates: listWorkflowTemplates,
	get: getWorkflow,
	create: createWorkflow,
	update: updateWorkflow,
	delete: deleteWorkflow,
	startRun,
	listRuns,
	getRun,
	approveRun,
	cancelRun,
	retryRun,
	testCredential,
	openRunStream,
};
