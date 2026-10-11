import type { Edge, Node } from '@xyflow/react';
import { Background, BackgroundVariant, Controls, ReactFlow } from '@xyflow/react';
import * as React from 'react';

import { nodeTypes } from './nodeTypes';
import { THEME_VARS } from './rfTheme';
import { edgeSatisfied, nodeStatus, readyNodeIds } from './runReadiness';
import type { AgentView, NodeRunState, WorkflowGraph } from '@/api';

interface RunGraphViewProps {
	/** The run's graph snapshot. */
	graph: WorkflowGraph;
	/** Node id → progress, the live patch target. */
	nodeStates: Record<string, NodeRunState>;
	/** Agent list so summaries match the builder's. */
	agents: AgentView[];
	/** The panel-selected node, mirrored onto the canvas. */
	selectedNodeId: string | null;
	onSelectNode: (nodeId: string | null) => void;
}

/**
 * The run detail's read-only graph: the same custom nodes as the
 * builder (`nodeTypes`, plus a status decoration their data accepts),
 * frozen in place — dragging, connecting and deleting are off; only
 * selection routes to the output panel. Edges animate while their data
 * is flowing: source SUCCEEDED, branch matched, target ready (the
 * planner's own frontier computation, ported in `runReadiness`).
 */
export function RunGraphView({
	graph,
	nodeStates,
	agents,
	selectedNodeId,
	onSelectNode,
}: RunGraphViewProps) {
	const ready = React.useMemo(() => readyNodeIds(graph, nodeStates), [graph, nodeStates]);

	const nodes = React.useMemo<Node[]>(
		() =>
			graph.nodes.map((node) => ({
				id: node.id,
				type: node.config.type,
				position: node.position ?? { x: 0, y: 0 },
				data: {
					config: node.config,
					agents,
					status: nodeStatus(nodeStates, node.id),
				},
				selected: node.id === selectedNodeId,
			})),
		[graph.nodes, nodeStates, agents, selectedNodeId],
	);

	const edges = React.useMemo<Edge[]>(
		() =>
			(graph.edges ?? []).map((edge) => ({
				id: edge.id,
				source: edge.source,
				target: edge.target,
				sourceHandle: edge.branch ?? null,
				label: edge.branch ?? undefined,
				animated: edgeSatisfied(edge, nodeStates) && ready.has(edge.target),
			})),
		[graph.edges, nodeStates, ready],
	);

	return (
		<div className="relative size-full" style={THEME_VARS}>
			<ReactFlow
				nodes={nodes}
				edges={edges}
				nodeTypes={nodeTypes}
				nodesDraggable={false}
				nodesConnectable={false}
				elementsSelectable
				deleteKeyCode={null}
				edgesFocusable={false}
				edgesReconnectable={false}
				onNodeClick={(_, node) => onSelectNode(node.id)}
				onPaneClick={() => onSelectNode(null)}
				fitView
				minZoom={0.2}
				maxZoom={2}
			>
				<Background variant={BackgroundVariant.Dots} gap={16} size={1} />
				<Controls showInteractive={false} />
			</ReactFlow>
		</div>
	);
}
