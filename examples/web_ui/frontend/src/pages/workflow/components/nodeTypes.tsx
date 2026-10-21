/* eslint-disable react-refresh/only-export-components --
 * React Flow needs the node components and their `nodeTypes` registry
 * (plus the shared NODE_META) in one module; splitting them just to
 * please fast refresh would separate the registry from its entries. */
import type { Node, NodeProps } from '@xyflow/react';
import { Handle, Position } from '@xyflow/react';
import { Bot, GitBranch, Repeat, Send, UserCheck } from 'lucide-react';

import type { AgentView, ChannelRecord, NodeConfig, RunStatus } from '@/api';
import { cn } from '@/lib/utils';

/** The node kinds the palette can add (RF node type == config type). */
export type NodeKind = 'agent' | 'condition' | 'loop' | 'human_approval' | 'notify';

/** Data every builder node carries: its config plus the agent list the
 * summaries render agent names from (passed by value to keep the node
 * components pure — no context juggling inside the canvas). A type
 * alias (not an interface) so React Flow's `Record<string, unknown>`
 * data constraint accepts it via its implicit index signature. */
export type BuilderNodeData = {
	config: NodeConfig;
	agents: AgentView[];
	/** Same-by-value channel list, for the notify cards' summaries. */
	channels?: ChannelRecord[];
	/**
	 * The node's run status — set ONLY by the read-only run view; the
	 * builder passes none and the decoration below stays dormant.
	 */
	status?: RunStatus;
};

type BuilderNode<K extends NodeKind = NodeKind> = Node<
	BuilderNodeData & { config: Extract<NodeConfig, { type: K }> },
	K
>;

/** Per-kind visual identity: icon + left accent color. */
export const NODE_META: Record<
	NodeKind,
	{ icon: typeof Bot; accent: string; ring: string }
> = {
	agent: { icon: Bot, accent: 'bg-indigo-500', ring: 'ring-indigo-500/40' },
	condition: { icon: GitBranch, accent: 'bg-amber-500', ring: 'ring-amber-500/40' },
	loop: { icon: Repeat, accent: 'bg-teal-500', ring: 'ring-teal-500/40' },
	human_approval: {
		icon: UserCheck,
		accent: 'bg-violet-500',
		ring: 'ring-violet-500/40',
	},
	notify: { icon: Send, accent: 'bg-rose-500', ring: 'ring-rose-500/40' },
};

/** Shared house-styled look for every handle dot. */
const HANDLE_STYLE: React.CSSProperties = {
	width: 9,
	height: 9,
	background: 'var(--primary)',
	border: '2px solid var(--card)',
};

/**
 * Run-view decoration (dormant while `status` is undefined): border
 * tint and a header status dot per status — green SUCCEEDED, blue
 * RUNNING (dot pulses), amber WAITING_HUMAN, red FAILED, grey
 * CANCELLED/PENDING (PENDING keeps the plain builder border).
 */
const STATUS_BORDER: Record<RunStatus, string> = {
	PENDING: '',
	RUNNING: 'border-blue-500/70 dark:border-blue-400/70',
	WAITING_HUMAN: 'border-amber-500/70 dark:border-amber-400/70',
	SUCCEEDED: 'border-green-500/70 dark:border-green-500/70',
	FAILED: 'border-red-500/70 dark:border-red-400/70',
	CANCELLED: 'border-muted-foreground/40',
};

const STATUS_DOT: Record<RunStatus, string> = {
	PENDING: 'bg-muted-foreground/50',
	RUNNING: 'animate-pulse bg-blue-500 dark:bg-blue-400',
	WAITING_HUMAN: 'bg-amber-500 dark:bg-amber-400',
	SUCCEEDED: 'bg-green-500 dark:bg-green-500',
	FAILED: 'bg-red-500 dark:bg-red-400',
	CANCELLED: 'bg-muted-foreground/60',
};

/**
 * Geometry for the labeled source handles: the card header occupies a
 * fixed 40px (8 padding + 16 title + 2 gap + 14 summary), then each
 * label row is a fixed 16px starting after a 6px gap. A branch handle
 * therefore sits at `54 + 16 * i` px from the card top — the vertical
 * center of its label row.
 */
const HEADER_PX = 54;
const ROW_PX = 16;
const labelRowTop = (index: number) => HEADER_PX + ROW_PX * index;

/** One builder node card: accent stripe, id, one-line summary, handles. */
function NodeCard({
	id,
	config,
	selected,
	status,
	summary,
	branchLabels,
}: {
	id: string;
	config: NodeConfig;
	selected: boolean;
	/** Run status; undefined in the builder (no decoration). */
	status?: RunStatus;
	summary: string;
	branchLabels: string[];
}) {
	const meta = NODE_META[config.type];
	const Icon = meta.icon;
	// Unique labels in stable order (a duplicated branch label renders
	// one handle; validation flags the duplicate).
	const labels = [...new Set(branchLabels)];

	return (
		<div
			className={cn(
				'relative w-52 rounded-lg border bg-card px-3 py-2 shadow-sm',
				status != null && STATUS_BORDER[status],
				status === 'RUNNING' && 'ring-2 ring-blue-500/25',
				selected && `ring-2 ${meta.ring}`,
			)}
		>
			<div className={cn('absolute inset-y-0 left-0 w-1 rounded-l-lg', meta.accent)} />

			<Handle type="target" position={Position.Left} style={HANDLE_STYLE} />

			<div className="flex h-4 items-center gap-1.5 pl-1.5">
				<Icon className="size-3.5 shrink-0 text-muted-foreground" />
				<span className="truncate text-xs font-semibold">{id}</span>
				{status != null && (
					<span
						className={cn('ml-auto size-2 shrink-0 rounded-full', STATUS_DOT[status])}
						title={status}
					/>
				)}
			</div>
			<div className="mt-0.5 h-3.5 truncate pl-1.5 text-[10px] leading-3.5 text-muted-foreground">
				{summary}
			</div>

			{labels.length > 0 && (
				<div className="mt-1.5">
					{labels.map((label) => (
						<div key={label} className="flex h-4 items-center justify-end">
							<span className="max-w-full truncate text-[9px] leading-none text-muted-foreground">
								{label}
							</span>
						</div>
					))}
				</div>
			)}

			{labels.length > 0 ? (
				labels.map((label, i) => (
					<Handle
						key={label}
						id={label}
						type="source"
						position={Position.Right}
						style={{ ...HANDLE_STYLE, top: labelRowTop(i) }}
					/>
				))
			) : (
				<Handle type="source" position={Position.Right} style={HANDLE_STYLE} />
			)}
		</div>
	);
}

function agentName(agents: AgentView[], agentId: string): string {
	if (!agentId) return '—';
	return agents.find((a) => a.id === agentId)?.data.name ?? agentId;
}

/** Same resolution as `agentName`, over the channel list (name ?? id). */
function channelName(channels: ChannelRecord[], channelId: string): string {
	if (!channelId) return '—';
	return channels.find((c) => c.id === channelId)?.name ?? channelId;
}

function AgentNode(props: NodeProps<BuilderNode<'agent'>>) {
	const { config, agents, status } = props.data;
	const hint = config.prompt_template?.trim() || '{{input}}';
	return (
		<NodeCard
			id={props.id}
			config={config}
			selected={props.selected}
			status={status}
			summary={`${agentName(agents, config.agent_id)} · ${hint}`}
			branchLabels={[]}
		/>
	);
}

function ConditionNode(props: NodeProps<BuilderNode<'condition'>>) {
	const { config, status } = props.data;
	return (
		<NodeCard
			id={props.id}
			config={config}
			selected={props.selected}
			status={status}
			summary={config.branches.map((b) => b.label).join(' · ') || '—'}
			branchLabels={config.branches.map((b) => b.label)}
		/>
	);
}

function LoopNode(props: NodeProps<BuilderNode<'loop'>>) {
	const { config, agents, status } = props.data;
	const summary =
		config.mode === 'iterate'
			? `iterate · ${config.list_template?.trim() || 'list'}`
			: `retry · ${agentName(agents, config.body_agent_id ?? '')}`;
	return (
		<NodeCard
			id={props.id}
			config={config}
			selected={props.selected}
			status={status}
			summary={summary}
			branchLabels={[]}
		/>
	);
}

function ApprovalNode(props: NodeProps<BuilderNode<'human_approval'>>) {
	const { config, status } = props.data;
	const approvers = config.approvers ?? [];
	const who = approvers.length > 0 ? `${approvers.length} approvers` : 'owner';
	const hours = config.timeout_hours ?? 24;
	const summary =
		config.timeout_branch && config.timeout_branch !== 'fail'
			? `${who} · ${hours}h → ${config.timeout_branch}`
			: `${who} · ${hours}h`;
	const labels = ['approve', 'reject'];
	if (config.timeout_branch && config.timeout_branch !== 'fail') {
		labels.push(config.timeout_branch);
	}
	return (
		<NodeCard
			id={props.id}
			config={config}
			selected={props.selected}
			status={status}
			summary={summary}
			branchLabels={labels}
		/>
	);
}

function NotifyNode(props: NodeProps<BuilderNode<'notify'>>) {
	const { config, channels, status } = props.data;
	const hint = config.message_template?.trim() || '{{input.message}}';
	return (
		<NodeCard
			id={props.id}
			config={config}
			selected={props.selected}
			status={status}
			summary={`${channelName(channels ?? [], config.channel_id)} · ${hint}`}
			branchLabels={[]}
		/>
	);
}

/** Registry handed to ReactFlow's `nodeTypes`; keys are config types. */
export const nodeTypes = {
	agent: AgentNode,
	condition: ConditionNode,
	loop: LoopNode,
	human_approval: ApprovalNode,
	notify: NotifyNode,
};
