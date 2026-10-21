import { ArrowLeft, LayoutTemplate } from 'lucide-react';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';

import type { WorkflowGraph, WorkflowTemplateView } from '@/api';
import { AgentSelect } from '@/components/select/AgentSelect';
import { ChannelSelect } from '@/components/select/ChannelSelect';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { useAgents } from '@/hooks/useAgents';
import { useChannels } from '@/hooks/useChannels';
import { useCreateWorkflow, useWorkflowTemplates } from '@/hooks/useWorkflows';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
}

/**
 * Clone the template's graph with every slot's empty reference replaced
 * by its mapped record id. Positions, node types and edges pass through
 * untouched — `structuredClone` copies the graph wholesale and only the
 * two reference fields are rewritten on the cloned configs.
 */
function mapTemplateGraph(
	template: WorkflowTemplateView,
	agentMap: Record<string, string>,
	channelMap: Record<string, string>,
): WorkflowGraph {
	const graph = structuredClone(template.graph);
	for (const node of graph.nodes) {
		if (node.config.type === 'agent' && agentMap[node.id]) {
			node.config = { ...node.config, agent_id: agentMap[node.id] };
		} else if (node.config.type === 'notify' && channelMap[node.id]) {
			node.config = { ...node.config, channel_id: channelMap[node.id] };
		}
	}
	return graph;
}

/** One template card in the gallery step: name, category chip,
 * description and the node / slot counts. */
function TemplateCard({
	template,
	onPick,
}: {
	template: WorkflowTemplateView;
	onPick: () => void;
}) {
	const { t } = useTranslation();
	const counts = [
		t('workflow.templates.nodes', { count: template.graph.nodes.length }),
		t('workflow.templates.agentSlots', { count: template.agent_slots.length }),
		t('workflow.templates.channelSlots', { count: template.channel_slots.length }),
	].join(' · ');

	return (
		<button
			type="button"
			onClick={onPick}
			className="flex w-full items-start gap-3 rounded-lg border bg-card px-3.5 py-3 text-left transition-colors hover:border-ring hover:bg-accent"
		>
			<LayoutTemplate className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
			<span className="min-w-0 flex-1">
				<span className="flex items-center gap-2">
					<span className="truncate text-sm font-medium">{template.name}</span>
					<Badge variant="secondary" className="shrink-0 text-[10px]">
						{template.category}
					</Badge>
				</span>
				<span className="mt-1 block text-xs leading-snug text-muted-foreground">
					{template.description}
				</span>
				<span className="mt-1.5 block text-[10px] text-muted-foreground">{counts}</span>
			</span>
		</button>
	);
}

/**
 * The template gallery and its install flow, one dialog in two steps:
 * pick a shipped template, then map its agent and channel slots to real
 * records (name editable, prefilled from the template). Create disabled
 * until every slot is mapped; a successful create navigates straight
 * to the builder with the mapped graph already in place.
 */
export function TemplatesDialog({ open, onOpenChange }: Props) {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const { templates, loading, error, refetch } = useWorkflowTemplates();
	const { agents } = useAgents();
	const { channels } = useChannels();
	const createWorkflow = useCreateWorkflow();

	// Step two's state; `selected === null` is the gallery step.
	const [selected, setSelected] = React.useState<WorkflowTemplateView | null>(null);
	const [name, setName] = React.useState('');
	// Slot node_id → mapped record id; a slot is mapped when truthy.
	const [agentMap, setAgentMap] = React.useState<Record<string, string>>({});
	const [channelMap, setChannelMap] = React.useState<Record<string, string>>({});

	// Reset both steps when the dialog closes, so the next open starts
	// at the gallery (the house reset-on-close pattern).
	React.useEffect(() => {
		if (open) return;
		setSelected(null);
		setName('');
		setAgentMap({});
		setChannelMap({});
	}, [open]);

	const hasEnabledChannel = channels.some((c) => c.enabled);
	const allMapped =
		selected !== null &&
		selected.agent_slots.every((slot) => agentMap[slot.node_id]) &&
		selected.channel_slots.every((slot) => channelMap[slot.node_id]);
	const canCreate = selected !== null && allMapped && name.trim() !== '';

	const pick = (template: WorkflowTemplateView) => {
		setSelected(template);
		setName(template.name);
	};

	const handleCreate = async () => {
		if (!selected || !canCreate || createWorkflow.isPending) return;
		try {
			const res = await createWorkflow.mutateAsync({
				name: name.trim(),
				description: selected.description,
				graph: mapTemplateGraph(selected, agentMap, channelMap),
			});
			onOpenChange(false);
			navigate(`/workflow/builder/${res.workflow_id}`);
		} catch {
			// The API client already toasted the failure detail; the
			// dialog stays open so the mapping can be adjusted.
		}
	};

	// ─── Step 1: the gallery ─────────────────────────────────────────────

	if (selected === null) {
		return (
			<Dialog open={open} onOpenChange={onOpenChange}>
				<DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
					<DialogHeader>
						<DialogTitle>{t('workflow.templates.title')}</DialogTitle>
						<DialogDescription>{t('workflow.templates.description')}</DialogDescription>
					</DialogHeader>

					{loading ? (
						<div className="flex justify-center py-6">
							<Spinner className="size-5" />
						</div>
					) : error ? (
						// The client-wide toast already reported the failure;
						// this inline state keeps the gallery actionable.
						<div className="space-y-2 py-2 text-center">
							<p className="text-xs text-muted-foreground">
								{t('workflow.templates.loadFailed')}
							</p>
							<Button variant="outline" size="sm" onClick={refetch}>
								{t('workflow.templates.retry')}
							</Button>
						</div>
					) : (
						<div className="space-y-2.5">
							{templates.map((template) => (
								<TemplateCard
									key={template.id}
									template={template}
									onPick={() => pick(template)}
								/>
							))}
						</div>
					)}
				</DialogContent>
			</Dialog>
		);
	}

	// ─── Step 2: map the slots ───────────────────────────────────────────

	const renderSlot = (
		slot: WorkflowTemplateView['agent_slots'][number],
		value: string | undefined,
		onChange: (id: string) => void,
		kind: 'agent' | 'channel',
	) => (
		<Field key={slot.node_id}>
			<FieldLabel className="flex items-baseline justify-between gap-2 text-xs">
				<span className="min-w-0">{slot.label}</span>
				<span className="shrink-0 font-mono text-[10px] font-normal text-muted-foreground">
					{slot.node_id}
				</span>
			</FieldLabel>
			{slot.hint && (
				<p className="text-[11px] leading-snug text-muted-foreground">{slot.hint}</p>
			)}
			{kind === 'agent' ? (
				<AgentSelect
					className="w-full"
					agents={agents}
					value={value ?? null}
					placeholder={t('workflow.builder.config.agent.selectPlaceholder')}
					onChange={onChange}
				/>
			) : (
				<ChannelSelect
					className="w-full"
					channels={channels}
					value={value ?? null}
					placeholder={t('workflow.builder.config.notify.channelPlaceholder')}
					onChange={onChange}
				/>
			)}
		</Field>
	);

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
				<DialogHeader>
					<DialogTitle>{selected.name}</DialogTitle>
					<DialogDescription>{selected.description}</DialogDescription>
				</DialogHeader>

				<FieldGroup>
					<Field>
						<FieldLabel>{t('workflow.templates.nameLabel')}</FieldLabel>
						<Input
							value={name}
							disabled={createWorkflow.isPending}
							onChange={(e) => setName(e.target.value)}
						/>
					</Field>

					{selected.agent_slots.length > 0 && (
						<section className="space-y-3">
							<div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
								{t('workflow.templates.agentTitle')}
							</div>
							{selected.agent_slots.map((slot) =>
								renderSlot(slot, agentMap[slot.node_id], (agentId) =>
									setAgentMap((map) => ({ ...map, [slot.node_id]: agentId })),
								'agent'),
							)}
						</section>
					)}

					{selected.channel_slots.length > 0 && (
						<section className="space-y-3">
							<div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
								{t('workflow.templates.channelTitle')}
							</div>
							{selected.channel_slots.map((slot) =>
								renderSlot(slot, channelMap[slot.node_id], (channelId) =>
									setChannelMap((map) => ({ ...map, [slot.node_id]: channelId })),
								'channel'),
							)}
							{!hasEnabledChannel && (
								// No enabled channels: nothing to map onto — the
								// create stays blocked, so say why.
								<p className="text-[11px] leading-snug text-muted-foreground">
									{t('workflow.builder.config.notify.channelEmptyHint')}
								</p>
							)}
						</section>
					)}

					{selected.agent_slots.length + selected.channel_slots.length === 0 && (
						<p className="text-xs text-muted-foreground">
							{t('workflow.templates.noSlots')}
						</p>
					)}
				</FieldGroup>

				<DialogFooter className="sm:justify-between">
					<Button
						variant="ghost"
						disabled={createWorkflow.isPending}
						onClick={() => setSelected(null)}
					>
						<ArrowLeft />
						{t('workflow.templates.back')}
					</Button>
					<div className="flex gap-2">
						<Button
							variant="ghost"
							disabled={createWorkflow.isPending}
							onClick={() => onOpenChange(false)}
						>
							{t('common.cancel')}
						</Button>
						<Button
							disabled={!canCreate || createWorkflow.isPending}
							onClick={() => void handleCreate()}
						>
							{createWorkflow.isPending && <Spinner className="size-3.5" />}
							{createWorkflow.isPending
								? t('workflow.templates.creating')
								: t('workflow.templates.create')}
						</Button>
					</div>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
