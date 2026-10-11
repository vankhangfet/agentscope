import type { NodeRunState, RunStatus, WorkflowEdge, WorkflowGraph } from '@/api';

/**
 * Client-side port of the engine's frontier computation
 * (`src/agentscope/app/_service/_workflow_planner.py`): which nodes may
 * dispatch now, and which edge actually carried data. The run view uses
 * it to tint nodes by status and animate the edges into ready nodes —
 * the same semantics the planner dispatches by, so the picture never
 * disagrees with what the engine does next.
 */

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
