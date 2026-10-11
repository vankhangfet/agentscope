import type {
	Connection,
	Edge,
	EdgeChange,
	Node,
	NodeChange,
	ReactFlowInstance,
	XYPosition,
} from '@xyflow/react';
import {
	Background,
	BackgroundVariant,
	Controls,
	ReactFlow,
	useReactFlow,
} from '@xyflow/react';
import * as React from 'react';

import { nodeTypes, type NodeKind } from './nodeTypes';

/** MIME type the palette writes on drag start (React Flow's dnd pattern). */
export const PALETTE_MIME = 'application/reactflow';

/**
 * House-theme overrides for React Flow's CSS variables, so the edges,
 * controls, handles and dot grid follow the light/dark tokens instead
 * of the library's fixed greys.
 */
const THEME_VARS = {
	'--xy-edge-stroke': 'var(--muted-foreground)',
	'--xy-edge-stroke-selected': 'var(--primary)',
	'--xy-edge-label-color': 'var(--foreground)',
	'--xy-edge-label-background-color': 'var(--card)',
	'--xy-controls-button-background-color': 'var(--card)',
	'--xy-controls-button-background-color-hover': 'var(--accent)',
	'--xy-controls-button-border-color': 'var(--border)',
	'--xy-controls-button-color': 'var(--foreground)',
	'--xy-background-pattern-dots-color': 'var(--border)',
	'--xy-handle-background-color': 'var(--primary)',
	'--xy-handle-border-color': 'var(--card)',
} as React.CSSProperties;

interface WorkflowCanvasProps {
	nodes: Node[];
	edges: Edge[];
	/** Applied to the graph in the page (remove/select/position). */
	onNodesChange: (changes: NodeChange[]) => void;
	/** Applied to the graph in the page (remove/select). */
	onEdgesChange: (changes: EdgeChange[]) => void;
	onConnect: (connection: Connection) => void;
	/** A palette item was dropped at this flow position. */
	onDropNode: (kind: NodeKind, position: XYPosition) => void;
	onInit: (instance: ReactFlowInstance) => void;
}

/**
 * The React Flow canvas. All state lives in the page (the graph is the
 * single source of truth); this component only renders the controlled
 * flow, maps HTML5 drops onto flow coordinates, and themes the chrome.
 * Must render inside a `ReactFlowProvider` (the page provides it).
 */
export function WorkflowCanvas({
	nodes,
	edges,
	onNodesChange,
	onEdgesChange,
	onConnect,
	onDropNode,
	onInit,
}: WorkflowCanvasProps) {
	const { screenToFlowPosition } = useReactFlow();

	const onDragOver = (event: React.DragEvent) => {
		if (!event.dataTransfer.types.includes(PALETTE_MIME)) return;
		event.preventDefault();
		event.dataTransfer.dropEffect = 'move';
	};

	const onDrop = (event: React.DragEvent) => {
		const kind = event.dataTransfer.getData(PALETTE_MIME) as NodeKind;
		if (!kind) return;
		event.preventDefault();
		onDropNode(kind, screenToFlowPosition({ x: event.clientX, y: event.clientY }));
	};

	return (
		<div className="relative size-full" style={THEME_VARS} onDrop={onDrop} onDragOver={onDragOver}>
			<ReactFlow
				nodes={nodes}
				edges={edges}
				nodeTypes={nodeTypes}
				onNodesChange={onNodesChange}
				onEdgesChange={onEdgesChange}
				onConnect={onConnect}
				onInit={onInit}
				fitView
				minZoom={0.2}
				maxZoom={2}
				// Backspace (RF default) plus Delete, so both remove a
				// selection; RF ignores these while typing in inputs.
				deleteKeyCode={['Backspace', 'Delete']}
			>
				<Background variant={BackgroundVariant.Dots} gap={16} size={1} />
				<Controls showInteractive={false} />
			</ReactFlow>
		</div>
	);
}
