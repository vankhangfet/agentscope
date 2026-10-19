import '@xyflow/react/dist/style.css';

import type {
	Connection,
	Edge,
	EdgeChange,
	Node,
	NodeChange,
	ReactFlowInstance,
	XYPosition,
} from '@xyflow/react';
import { ReactFlowProvider, useReactFlow } from '@xyflow/react';
import { Play, Save, TriangleAlert, Workflow, X } from 'lucide-react';
import * as React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { NodeConfigPanel } from './components/NodeConfigPanel';
import { NodePalette } from './components/NodePalette';
import type { NodeKind } from './components/nodeTypes';
import { RunWorkflowDialog } from './components/RunWorkflowDialog';
import { NODE_ID_PATTERN, validateGraph } from './components/validateGraph';
import { WorkflowCanvas } from './components/WorkflowCanvas';
import type { NodeConfig, WorkflowData, WorkflowGraph } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Empty,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { useAgents } from '@/hooks/useAgents';
import {
	useCreateWorkflow,
	useUpdateWorkflow,
	useWorkflow,
} from '@/hooks/useWorkflows';
import { useTranslation } from '@/i18n/useI18n';

/** First free id of the form `node1`, `node2`, … (edges: `edge1`, …). */
function nextFreeId(prefix: string, taken: Iterable<string>): string {
	const used = new Set(taken);
	let i = 1;
	while (used.has(`${prefix}${i}`)) i++;
	return `${prefix}${i}`;
}

/** Backend defaults, so a fresh node saves unchanged where possible. */
function defaultConfig(kind: NodeKind): NodeConfig {
	switch (kind) {
		case 'agent':
			return { type: 'agent', agent_id: '', prompt_template: '{{input}}', max_retries: 2 };
		case 'condition':
			return {
				type: 'condition',
				branches: [{ label: 'then', rule: '' }],
				chat_model_config: null,
			};
		case 'loop':
			return {
				type: 'loop',
				mode: 'iterate',
				list_template: '',
				item_template: '{{item}}',
				body_agent_id: '',
				verifier_prompt: '',
				max_attempts: 3,
				parallel: false,
			};
		case 'human_approval':
			return {
				type: 'human_approval',
				approvers: [],
				timeout_hours: 24,
				timeout_branch: 'fail',
			};
	}
}

/**
 * Canonical text of the editor's persisted fields — the same projection
 * `handleSave` sends — so "unsaved changes" is a plain string compare
 * against the last seeded/saved snapshot.
 */
function serializeEditor(name: string, description: string, graph: WorkflowGraph): string {
	return JSON.stringify({
		// Saved names are trimmed server-side; trim here too so a
		// trailing space right after save doesn't read as a new edit.
		name: name.trim(),
		description,
		nodes: graph.nodes.map(({ id, position, config }) => ({ id, position, config })),
		edges: graph.edges ?? [],
	});
}

/**
 * Detects a single label rename between two branch label lists, so an
 * edge wired to the old label can follow the rename. Null when the
 * change is anything other than exactly one in-place rename.
 */
function detectLabelRename(
	oldLabels: string[],
	newLabels: string[],
): [string, string] | null {
	if (oldLabels.length !== newLabels.length) return null;
	let rename: [string, string] | null = null;
	for (let i = 0; i < oldLabels.length; i++) {
		if (oldLabels[i] === newLabels[i]) continue;
		if (rename !== null) return null;
		rename = [oldLabels[i], newLabels[i]];
	}
	if (rename === null) return null;
	const [from, to] = rename;
	if (!from || !to) return null;
	// Renaming onto another existing label would merge two arms —
	// leave the edges alone and let validation flag the duplicate.
	if (oldLabels.includes(to)) return null;
	return rename;
}

function Builder() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const { workflowId: routeId } = useParams<{ workflowId?: string }>();
	const workflowId = routeId ? routeId : null;
	const { workflow, loading, error } = useWorkflow(workflowId);
	const { agents } = useAgents();
	const createWorkflow = useCreateWorkflow();
	const updateWorkflow = useUpdateWorkflow();
	const { screenToFlowPosition } = useReactFlow();

	// The graph is the single source of truth; React Flow renders a
	// projection of it and every canvas change maps back onto it.
	const [graph, setGraph] = React.useState<WorkflowGraph>({ nodes: [], edges: [] });
	const [name, setName] = React.useState('');
	const [description, setDescription] = React.useState('');
	const [version, setVersion] = React.useState<number | null>(null);
	// Canvas selection is presentation state, mirrored onto the derived
	// RF nodes/edges so selection survives graph updates.
	const [selectedNodeIds, setSelectedNodeIds] = React.useState<string[]>([]);
	const [selectedEdgeIds, setSelectedEdgeIds] = React.useState<string[]>([]);
	const [showProblems, setShowProblems] = React.useState(false);
	const [saving, setSaving] = React.useState(false);
	const [runOpen, setRunOpen] = React.useState(false);
	// The editor state as last seeded or saved; drift against it is the
	// "unsaved changes" that keeps the Run button disabled. State, not a
	// ref, because the render-time comparison reads it.
	const [cleanSnapshot, setCleanSnapshot] = React.useState('');

	const rfInstance = React.useRef<ReactFlowInstance | undefined>(undefined);
	const canvasRef = React.useRef<HTMLDivElement | null>(null);

	// Seed the editor once per loaded workflow — the guard keeps query
	// refetches (e.g. after save) from clobbering local edits.
	const seededFor = React.useRef<string | null>(null);
	React.useEffect(() => {
		if (!workflowId || !workflow) return;
		if (seededFor.current === workflowId) return;
		seededFor.current = workflowId;
		setGraph(structuredClone(workflow.graph));
		setName(workflow.name);
		setDescription(workflow.description ?? '');
		setVersion(workflow.version ?? null);
		setCleanSnapshot(
			serializeEditor(workflow.name, workflow.description ?? '', workflow.graph),
		);
		// The graph arrives after mount, so fitView-on-init missed it.
		requestAnimationFrame(() =>
			rfInstance.current?.fitView({ padding: 0.25, maxZoom: 1 }),
		);
	}, [workflowId, workflow]);

	const problems = React.useMemo(() => validateGraph(graph), [graph]);

	// Drift between the editor and the last seeded/saved snapshot. Runs
	// execute the stored workflow, so unsaved edits disable Run.
	const dirty = serializeEditor(name, description, graph) !== cleanSnapshot;

	// Pop the problems panel open when issues first appear, so the
	// disabled Save button is never a mystery.
	const prevProblemCount = React.useRef(0);
	React.useEffect(() => {
		if (problems.length > 0 && prevProblemCount.current === 0) setShowProblems(true);
		prevProblemCount.current = problems.length;
	}, [problems]);

	// ─── Graph mutations ─────────────────────────────────────────────────

	const addNode = React.useCallback(
		(kind: NodeKind, position: XYPosition) => {
			const id = nextFreeId('node', graph.nodes.map((n) => n.id));
			setGraph((g) => ({ ...g, nodes: [...g.nodes, { id, position, config: defaultConfig(kind) }] }));
			setSelectedNodeIds([id]);
			setSelectedEdgeIds([]);
		},
		[graph.nodes],
	);

	const onNodesChange = React.useCallback((changes: NodeChange[]) => {
		const removed = new Set<string>();
		const positions = new Map<string, XYPosition>();
		for (const change of changes) {
			if (change.type === 'remove') {
				removed.add(change.id);
			} else if (change.type === 'position' && change.position) {
				positions.set(change.id, change.position);
			} else if (change.type === 'select') {
				setSelectedNodeIds((ids) =>
					change.selected ? [...new Set([...ids, change.id])] : ids.filter((i) => i !== change.id),
				);
			}
		}
		if (removed.size === 0 && positions.size === 0) return;
		setGraph((g) => {
			let nodes = g.nodes;
			let edges = g.edges ?? [];
			if (removed.size > 0) {
				nodes = nodes.filter((n) => !removed.has(n.id));
				edges = edges.filter((e) => !removed.has(e.source) && !removed.has(e.target));
			}
			if (positions.size > 0) {
				nodes = nodes.map((n) =>
					positions.has(n.id) ? { ...n, position: positions.get(n.id) } : n,
				);
			}
			return { ...g, nodes, edges };
		});
	}, []);

	const onEdgesChange = React.useCallback((changes: EdgeChange[]) => {
		const removed = new Set<string>();
		for (const change of changes) {
			if (change.type === 'remove') {
				removed.add(change.id);
			} else if (change.type === 'select') {
				setSelectedEdgeIds((ids) =>
					change.selected ? [...new Set([...ids, change.id])] : ids.filter((i) => i !== change.id),
				);
			}
		}
		if (removed.size === 0) return;
		setGraph((g) => ({ ...g, edges: (g.edges ?? []).filter((e) => !removed.has(e.id)) }));
	}, []);

	const onConnect = React.useCallback((connection: Connection) => {
		setGraph((g) => {
			const edges = g.edges ?? [];
			// A branch handle's id (condition/approval) rides along as
			// the edge's branch; the default handle yields null.
			const edge = {
				id: nextFreeId('edge', edges.map((e) => e.id)),
				source: connection.source,
				target: connection.target,
				branch: connection.sourceHandle ?? null,
			};
			return { ...g, edges: [...edges, edge] };
		});
	}, []);

	const renameNode = React.useCallback((oldId: string, nextId: string) => {
		if (nextId === oldId || !NODE_ID_PATTERN.test(nextId)) return;
		setGraph((g) => {
			if (g.nodes.some((n) => n.id === nextId)) return g;
			return {
				nodes: g.nodes.map((n) => (n.id === oldId ? { ...n, id: nextId } : n)),
				edges: (g.edges ?? []).map((e) => ({
					...e,
					source: e.source === oldId ? nextId : e.source,
					target: e.target === oldId ? nextId : e.target,
				})),
			};
		});
		setSelectedNodeIds((ids) => ids.map((id) => (id === oldId ? nextId : id)));
	}, []);

	const updateConfig = React.useCallback((nodeId: string, next: NodeConfig) => {
		setGraph((g) => {
			const previous = g.nodes.find((n) => n.id === nodeId)?.config;
			let edges = g.edges ?? [];
			if (next.type === 'condition' && previous?.type === 'condition') {
				const rename = detectLabelRename(
					previous.branches.map((b) => b.label),
					next.branches.map((b) => b.label),
				);
				if (rename) {
					const [from, to] = rename;
					edges = edges.map((e) =>
						e.source === nodeId && e.branch === from ? { ...e, branch: to } : e,
					);
				}
			}
			return {
				...g,
				nodes: g.nodes.map((n) => (n.id === nodeId ? { ...n, config: next } : n)),
				edges,
			};
		});
	}, []);

	// ─── React Flow projection ───────────────────────────────────────────

	const rfNodes = React.useMemo<Node[]>(
		() =>
			graph.nodes.map((n) => ({
				id: n.id,
				type: n.config.type,
				position: n.position ?? { x: 0, y: 0 },
				data: { config: n.config, agents },
				selected: selectedNodeIds.includes(n.id),
			})),
		[graph.nodes, agents, selectedNodeIds],
	);

	const rfEdges = React.useMemo<Edge[]>(
		() =>
			(graph.edges ?? []).map((e) => ({
				id: e.id,
				source: e.source,
				target: e.target,
				sourceHandle: e.branch ?? null,
				label: e.branch ?? undefined,
				selected: selectedEdgeIds.includes(e.id),
			})),
		[graph.edges, selectedEdgeIds],
	);

	const selectedNode =
		selectedNodeIds.length === 1
			? (graph.nodes.find((n) => n.id === selectedNodeIds[0]) ?? null)
			: null;

	// ─── Palette interactions ────────────────────────────────────────────

	const addAtCenter = React.useCallback(
		(kind: NodeKind) => {
			const rect = canvasRef.current?.getBoundingClientRect();
			const jitter = () => Math.random() * 48 - 24;
			const position = rect
				? screenToFlowPosition({
						x: rect.left + rect.width / 2 + jitter(),
						y: rect.top + rect.height / 2 + jitter(),
					})
				: { x: 40, y: 40 };
			addNode(kind, position);
		},
		[addNode, screenToFlowPosition],
	);

	// ─── Save ────────────────────────────────────────────────────────────

	const saveDisabled =
		saving || loading || !name.trim() || graph.nodes.length === 0 || problems.length > 0;

	// ─── Run ────────────────────────────────────────────────────────────

	// A run executes the stored workflow, so it needs a saved, valid
	// graph: a failed load (whose unseeded snapshot would otherwise read
	// as "unsaved"), brand-new or edited-but-unsaved, and any validation
	// problem all keep the button disabled — with the reason as its
	// tooltip.
	const runDisabled = loading || error !== null || !workflowId || dirty || problems.length > 0;
	const runReason = loading
		? undefined
		: error !== null
			? // Same copy as the canvas's load-failure empty state.
				t('workflow.builder.loadFailed')
			: !workflowId || dirty
				? t('workflow.builder.runUnsaved')
				: problems.length > 0
					? t('workflow.builder.runProblems', { count: problems.length })
					: undefined;

	const handleSave = async () => {
		if (saveDisabled) return;
		setSaving(true);
		try {
			const data: WorkflowData = {
				name: name.trim(),
				description,
				graph: {
					nodes: graph.nodes.map(({ id, position, config }) => ({ id, position, config })),
					edges: graph.edges ?? [],
				},
			};
			if (workflowId) {
				const res = await updateWorkflow.mutateAsync({ workflowId, data });
				setVersion(res.data.version ?? null);
				toast.success(t('workflow.builder.saved'));
			} else {
				const res = await createWorkflow.mutateAsync(data);
				toast.success(t('workflow.builder.saved'));
				// The editor already holds the just-saved graph; mark the
				// new id as seeded before navigating so the workflow query
				// the route change triggers cannot re-seed over it.
				seededFor.current = res.workflow_id;
				navigate(`/workflow/builder/${res.workflow_id}`, { replace: true });
			}
			// What was just persisted is the new clean state.
			setCleanSnapshot(serializeEditor(data.name, data.description ?? '', data.graph));
		} catch {
			// The API client already toasted the failure detail.
		} finally {
			setSaving(false);
		}
	};

	// ─── Render ──────────────────────────────────────────────────────────

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<header className="flex items-center justify-between gap-3 border-b px-4 py-2.5">
					<div className="flex min-w-0 items-center gap-2">
						<Input
							value={name}
							placeholder={t('workflow.builder.namePlaceholder')}
							className="h-8 w-64 border-transparent bg-transparent px-2 text-base font-semibold"
							onChange={(e) => setName(e.target.value)}
						/>
						{version != null && (
							<Badge variant="secondary">
								{t('workflow.list.version', { version })}
							</Badge>
						)}
					</div>
					<div className="flex shrink-0 items-center gap-2">
						<Button
							variant="outline"
							size="sm"
							className={
								problems.length > 0 ? 'border-destructive/40 text-destructive' : ''
							}
							onClick={() => setShowProblems((v) => !v)}
						>
							<TriangleAlert />
							{t('workflow.builder.problems')}
							{problems.length > 0 && (
								<Badge variant="destructive" className="ml-1 px-1.5">
									{problems.length}
								</Badge>
							)}
						</Button>
						{/* `pointer-events-auto` keeps the reason tooltip
						    hoverable while the button sits disabled. */}
						<Button
							variant="outline"
							size="sm"
							className="disabled:pointer-events-auto"
							disabled={runDisabled}
							tooltip={runReason}
							onClick={() => setRunOpen(true)}
						>
							<Play />
							{t('workflow.builder.run')}
						</Button>
						<Button size="sm" disabled={saveDisabled} onClick={() => void handleSave()}>
							{saving ? <Spinner className="size-3.5" /> : <Save />}
							{saving ? t('workflow.builder.saving') : t('workflow.builder.save')}
						</Button>
					</div>
				</header>

				{showProblems && problems.length > 0 && (
					<section className="border-b bg-destructive/5 px-4 py-2">
						<div className="flex items-center justify-between gap-2">
							<span className="text-xs font-semibold text-destructive">
								{t('workflow.builder.problemsCount', { count: problems.length })}
							</span>
							<Button
								variant="ghost"
								size="icon-sm"
								className="text-muted-foreground"
								aria-label={t('workflow.builder.hideProblems')}
								onClick={() => setShowProblems(false)}
							>
								<X />
							</Button>
						</div>
						<ul className="mt-1 max-h-32 space-y-1 overflow-y-auto">
							{problems.map((problem, i) => (
								<li key={i} className="font-mono text-[11px] text-destructive">
									{problem}
								</li>
							))}
						</ul>
					</section>
				)}

				<div className="flex min-h-0 flex-1">
					<NodePalette onAdd={addAtCenter} />

					<div ref={canvasRef} className="relative min-w-0 flex-1">
						{loading ? (
							<div className="flex size-full items-center justify-center">
								<Spinner className="size-6" />
							</div>
						) : error ? (
							<Empty className="border-none py-10">
								<EmptyHeader>
									<EmptyMedia variant="icon">
										<Workflow />
									</EmptyMedia>
									<EmptyTitle>{t('workflow.builder.loadFailed')}</EmptyTitle>
									<EmptyDescription>{error.message}</EmptyDescription>
								</EmptyHeader>
							</Empty>
						) : (
							<>
								<WorkflowCanvas
									nodes={rfNodes}
									edges={rfEdges}
									onNodesChange={onNodesChange}
									onEdgesChange={onEdgesChange}
									onConnect={onConnect}
									onDropNode={addNode}
									onInit={(instance) => {
										rfInstance.current = instance;
									}}
								/>
								{graph.nodes.length === 0 && (
									<div className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 text-muted-foreground">
										<Workflow className="size-8 opacity-40" />
										<p className="text-sm">{t('workflow.builder.emptyCanvas')}</p>
										<p className="text-xs">{t('workflow.builder.emptyCanvasHint')}</p>
									</div>
								)}
							</>
						)}
					</div>

					{selectedNode && (
						<NodeConfigPanel
							node={selectedNode}
							agents={agents}
							onRename={(nextId) => renameNode(selectedNode.id, nextId)}
							onConfigChange={(next) => updateConfig(selectedNode.id, next)}
							onClose={() => {
								setSelectedNodeIds([]);
								setSelectedEdgeIds([]);
							}}
						/>
					)}
				</div>
			</main>

			<RunWorkflowDialog
				workflowId={workflowId ?? undefined}
				open={runOpen}
				onOpenChange={setRunOpen}
			/>
		</div>
	);
}

/** The workflow builder: palette / canvas / config panel around a
 * React Flow graph that persists as a `WorkflowGraph`. */
export default function WorkflowBuilderPage() {
	return (
		<ReactFlowProvider>
			<Builder />
		</ReactFlowProvider>
	);
}
