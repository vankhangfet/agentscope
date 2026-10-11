import type {
	NodeRunState,
	RunStatus,
	WorkflowEdge,
	WorkflowGraph,
	WorkflowRunData,
} from '@/api';

/**
 * Shared run-view logic, centered on a client-side port of the engine's
 * frontier computation
 * (`src/agentscope/app/_service/_workflow_planner.py`): which nodes may
 * dispatch now, and which edge actually carried data. The run view uses
 * it to tint nodes by status and animate the edges into ready nodes —
 * the same semantics the planner dispatches by, so the picture never
 * disagrees with what the engine does next. The rest are the run-status
 * facts every view must derive identically: the terminal-status list
 * and the parked-node predicate.
 */

/**
 * Run statuses no further transition can follow — mirror of the
 * backend's `_RUN_TERMINAL_STATUSES` (a retry is a fresh dispatch).
 */
export const RUN_TERMINAL_STATUSES: readonly RunStatus[] = [
	'SUCCEEDED',
	'FAILED',
	'CANCELLED',
];

/**
 * The nodes a run is parked on for a human verdict: the run itself is
 * WAITING_HUMAN and the node's own state is WAITING_HUMAN. One entry
 * per parked node, in graph order — a run waiting on several approvals
 * yields several. The one predicate behind the approvals cards, the
 * sidebar chip, and the run view's approve block.
 */
export function parkedNodes(data: WorkflowRunData): { nodeId: string }[] {
	if (data.status !== 'WAITING_HUMAN') return [];
	return data.graph.nodes
		.filter((node) => data.node_states[node.id]?.status === 'WAITING_HUMAN')
		.map((node) => ({ nodeId: node.id }));
}

/** Statuses that bar a node from being (re)selected — mirror of `_BUSY`. */
const BUSY: ReadonlySet<RunStatus> = new Set([
	'RUNNING',
	'WAITING_HUMAN',
	'SUCCEEDED',
	'FAILED',
	'CANCELLED',
]);

/** A node's status, defaulting to PENDING before its first event. */
export function nodeStatus(
	nodeStates: Record<string, NodeRunState>,
	nodeId: string,
): RunStatus {
	return nodeStates[nodeId]?.status ?? 'PENDING';
}

/**
 * Whether an edge's data actually flowed: the source SUCCEEDED and —
 * when the edge carries a branch label — the source's output selected
 * that branch (`output["branch"]`). A FAILED or CANCELLED source
 * satisfies nothing.
 */
export function edgeSatisfied(
	edge: WorkflowEdge,
	nodeStates: Record<string, NodeRunState>,
): boolean {
	const source = nodeStates[edge.source];
	if (source?.status !== 'SUCCEEDED') return false;
	if (edge.branch == null) return true;
	return ((source.output ?? {}) as Record<string, unknown>).branch === edge.branch;
}

/**
 * Which nodes have every incoming edge satisfied and are not already
 * (or never again) dispatchable — the planner's `ready_nodes`.
 */
export function readyNodeIds(
	graph: WorkflowGraph,
	nodeStates: Record<string, NodeRunState>,
): Set<string> {
	const incoming = new Map<string, WorkflowEdge[]>(
		graph.nodes.map((node) => [node.id, []]),
	);
	for (const edge of graph.edges ?? []) {
		incoming.get(edge.target)?.push(edge);
	}

	const ready = new Set<string>();
	for (const node of graph.nodes) {
		if (BUSY.has(nodeStatus(nodeStates, node.id))) continue;
		let satisfied = true;
		for (const edge of incoming.get(node.id) ?? []) {
			if (!edgeSatisfied(edge, nodeStates)) {
				satisfied = false;
				break;
			}
		}
		if (satisfied) ready.add(node.id);
	}
	return ready;
}
