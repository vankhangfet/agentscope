import type { WorkflowEdge, WorkflowGraph, WorkflowNode } from '@/api';

/**
 * Client-side port of the backend's `validate_workflow_graph`
 * (`src/agentscope/app/storage/_model/_workflow.py`): the structural
 * checks every workflow must pass before saving. Returns a list of
 * human-readable problems (English, mirroring the backend's text);
 * empty means valid.
 *
 * Deliberately NOT ported here — the server re-validates on save and
 * surfaces these as 422 toasts: pydantic field constraints (branch
 * min-length, retry counts, timeout bounds, the remaining loop-mode
 * fields) and cross-record checks (agent existence). The one exception
 * is the iterate loop's list template: an empty one is common enough
 * mid-edit (the node ships with a blank template) that it is checked
 * here so Save disables and the problems strip explains why.
 */

/** Same pattern the backend enforces on node ids. */
export const NODE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

function countDuplicates(values: string[]): Map<string, number> {
	const counts = new Map<string, number>();
	for (const value of values) {
		counts.set(value, (counts.get(value) ?? 0) + 1);
	}
	return counts;
}

/** Python-style sorted list repr, e.g. `['a', 'b']`, for message parity. */
function formatList(values: Iterable<string>): string {
	return `[${[...values].sort().map((v) => `'${v}'`).join(', ')}]`;
}

/**
 * Coloured DFS over the known nodes' outgoing edges — a faithful port
 * of the backend's `_find_cycle` (recursion instead of the Python
 * explicit stack; same colouring, same cycle-path semantics),
 * returning the cycle path (start repeated at the end) or null.
 */
function findCycle(
	known: Set<string>,
	outgoing: Map<string, WorkflowEdge[]>,
): string[] | null {
	const WHITE = 0;
	const GRAY = 1;
	const BLACK = 2;
	const colour = new Map<string, number>([...known].map((id) => [id, WHITE]));
	const path: string[] = [];

	const visit = (id: string): string[] | null => {
		colour.set(id, GRAY);
		path.push(id);
		for (const edge of outgoing.get(id) ?? []) {
			const next = edge.target;
			if (colour.get(next) === GRAY) {
				return [...path.slice(path.indexOf(next)), next];
			}
			if (colour.get(next) === WHITE) {
				const cycle = visit(next);
				if (cycle !== null) return cycle;
			}
		}
		path.pop();
		colour.set(id, BLACK);
		return null;
	};

	for (const start of known) {
		if (colour.get(start) === WHITE) {
			const cycle = visit(start);
			if (cycle !== null) return cycle;
		}
	}
	return null;
}

/**
 * Port of the backend's `_check_branches`: match a branching node's
 * label set against its (known-endpoint) outgoing edges. Returns the
 * labels that actually have edges.
 */
function checkBranches(
	nodeId: string,
	out: WorkflowEdge[],
	labels: Set<string>,
	problems: string[],
	unlabeledIsError: boolean,
): Set<string> {
	const branchCounts = countDuplicates(
		out.filter((e) => e.branch != null).map((e) => e.branch as string),
	);
	for (const [label, count] of branchCounts) {
		if (count > 1) {
			problems.push(`Node ${nodeId} has ${count} edges for branch '${label}'`);
		}
	}
	const labelled = new Set(branchCounts.keys());
	for (const missing of labels) {
		if (!labelled.has(missing)) {
			problems.push(`Branching node ${nodeId} has no edge for branch '${missing}'`);
		}
	}
	for (const extra of labelled) {
		if (!labels.has(extra)) {
			problems.push(`Edge from ${nodeId} carries unknown branch '${extra}'`);
		}
	}
	if (unlabeledIsError && out.some((e) => e.branch == null)) {
		problems.push(`Branching node ${nodeId} has an unlabeled outgoing edge`);
	}
	return labelled;
}

/** Structural validation of a workflow graph; empty result means valid. */
export function validateGraph(graph: WorkflowGraph): string[] {
	const problems: string[] = [];
	const nodes: WorkflowNode[] = graph.nodes ?? [];
	const edges: WorkflowEdge[] = graph.edges ?? [];

	const nodeCounts = countDuplicates(nodes.map((n) => n.id));
	const duplicateNodes = [...nodeCounts.entries()]
		.filter(([, count]) => count > 1)
		.map(([id]) => id);
	if (duplicateNodes.length > 0) {
		problems.push(`Duplicate node ids: ${formatList(duplicateNodes)}`);
	}
	const known = new Set(nodeCounts.keys());

	for (const node of nodes) {
		if (!NODE_ID_PATTERN.test(node.id)) {
			problems.push(
				`Node id '${node.id}' is invalid: must start with an alphanumeric ` +
					`and continue with alphanumerics, dashes or underscores`,
			);
		}
	}

	const edgeCounts = countDuplicates(edges.map((e) => e.id));
	const duplicateEdges = [...edgeCounts.entries()]
		.filter(([, count]) => count > 1)
		.map(([id]) => id);
	if (duplicateEdges.length > 0) {
		problems.push(`Duplicate edge ids: ${formatList(duplicateEdges)}`);
	}

	const outgoing = new Map<string, WorkflowEdge[]>([...known].map((id) => [id, []]));
	for (const edge of edges) {
		if (!known.has(edge.source)) {
			problems.push(`Edge ${edge.id} starts at unknown node ${edge.source}`);
		}
		if (!known.has(edge.target)) {
			problems.push(`Edge ${edge.id} ends at unknown node ${edge.target}`);
		}
		if (known.has(edge.source) && known.has(edge.target)) {
			outgoing.get(edge.source)?.push(edge);
		}
	}

	const cycle = findCycle(known, outgoing);
	if (cycle !== null) {
		problems.push(`Cycle detected: ${cycle.join(' -> ')}`);
	}

	for (const node of nodes) {
		const out = outgoing.get(node.id) ?? [];
		if (node.config.type === 'condition') {
			const labels = node.config.branches.map((b) => b.label);
			const dupes = [...countDuplicates(labels).entries()]
				.filter(([, count]) => count > 1)
				.map(([label]) => label);
			if (dupes.length > 0) {
				problems.push(
					`Condition node ${node.id} has duplicate branch labels: ${formatList(dupes)}`,
				);
			}
			checkBranches(node.id, out, new Set(labels), problems, true);
		} else if (node.config.type === 'loop') {
			// Whitespace-only counts as empty: the node ships with a
			// blank template and iterate has nothing to fan out without
			// one (the backend rejects it on save, but only after a
			// round trip — this keeps the Problems strip honest).
			// Bare-id `<type> node <id> …` wording, matching the
			// Condition / Approval node messages above.
			if (node.config.mode === 'iterate' && !(node.config.list_template ?? '').trim()) {
				problems.push(`Loop node ${node.id} needs a list template`);
			}
		} else if (node.config.type === 'human_approval') {
			const allowed = new Set(['approve', 'reject']);
			if (node.config.timeout_branch !== 'fail') {
				allowed.add(node.config.timeout_branch as string);
			}
			const labelled = checkBranches(node.id, out, allowed, problems, true);
			if (
				node.config.timeout_branch !== 'fail' &&
				!labelled.has(node.config.timeout_branch as string)
			) {
				problems.push(
					`Approval node ${node.id} timeout_branch ` +
						`'${node.config.timeout_branch}' has no matching outgoing edge`,
				);
			}
		}
	}

	return problems;
}
