import { useQueryClient } from '@tanstack/react-query';
import { format } from 'date-fns';
import { Copy, Ellipsis, History, Pen, Play, Plus, Trash2, Workflow } from 'lucide-react';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import { RunWorkflowDialog } from './components/RunWorkflowDialog';
import type { Workflow as WorkflowRecord } from '@/api';
import { getWorkflow } from '@/api';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
	Empty,
	EmptyContent,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty';
import {
	Item,
	ItemActions,
	ItemContent,
	ItemDescription,
	ItemMedia,
	ItemTitle,
} from '@/components/ui/item';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import {
	useCreateWorkflow,
	useDeleteWorkflow,
	useWorkflows,
	WORKFLOWS_KEY,
} from '@/hooks/useWorkflows';
import { useTranslation } from '@/i18n/useI18n';

interface WorkflowCardProps {
	workflow: WorkflowRecord;
	/** True while this row's duplicate request is in flight. */
	duplicating: boolean;
	onOpen: () => void;
	onRun: () => void;
	onDuplicate: () => void;
	onViewRuns: () => void;
	onDelete: () => void;
}

/** One workflow in the list — the workflow sibling of `ScheduleCard`. */
function WorkflowCard({
	workflow,
	duplicating,
	onOpen,
	onRun,
	onDuplicate,
	onViewRuns,
	onDelete,
}: WorkflowCardProps) {
	const { t } = useTranslation();
	const { data } = workflow;

	return (
		<Item variant="outline" className="cursor-pointer hover:shadow-md" onClick={onOpen}>
			<ItemMedia variant="icon">
				<Workflow />
			</ItemMedia>
			<ItemContent>
				<ItemTitle className="max-w-full truncate">{data.name}</ItemTitle>
				<ItemDescription className="line-clamp-1">
					{data.description || '—'}
				</ItemDescription>
			</ItemContent>
			<ItemActions className="shrink-0">
				<span className="hidden font-mono text-[11px] text-muted-foreground md:inline">
					{format(new Date(workflow.updated_at), 'yyyy-MM-dd HH:mm')}
				</span>
				<Badge variant="secondary">
					{t('workflow.list.nodeCount', { count: data.graph.nodes.length })}
				</Badge>
				{data.version != null && (
					<Badge variant="outline">
						{t('workflow.list.version', { version: data.version })}
					</Badge>
				)}
				<DropdownMenu>
					<DropdownMenuTrigger asChild>
						<Button
							variant="ghost"
							size="icon-sm"
							className="text-text-tertiary hover:text-foreground"
							aria-label={t('workflow.list.actions.menu')}
							onClick={(e) => e.stopPropagation()}
						>
							<Ellipsis />
						</Button>
					</DropdownMenuTrigger>
					<DropdownMenuContent align="end">
						{/* The menu renders through a portal, but the React
						    parent chain — and so each click's propagation —
						    still runs through the card; stop it or the
						    card's navigation races the dialog an item
						    opens. */}
						<DropdownMenuItem
							onClick={(e) => {
								e.stopPropagation();
								onOpen();
							}}
						>
							<Pen />
							{t('workflow.list.actions.edit')}
						</DropdownMenuItem>
						<DropdownMenuItem
							onClick={(e) => {
								e.stopPropagation();
								onRun();
							}}
						>
							<Play />
							{t('workflow.list.actions.run')}
						</DropdownMenuItem>
						<DropdownMenuItem
							disabled={duplicating}
							onClick={(e) => {
								e.stopPropagation();
								onDuplicate();
							}}
						>
							<Copy />
							{t('workflow.list.actions.duplicate')}
						</DropdownMenuItem>
						<DropdownMenuItem
							onClick={(e) => {
								e.stopPropagation();
								onViewRuns();
							}}
						>
							<History />
							{t('workflow.list.actions.viewRuns')}
						</DropdownMenuItem>
						<DropdownMenuItem
							variant="destructive"
							onClick={(e) => {
								e.stopPropagation();
								onDelete();
							}}
						>
							<Trash2 />
							{t('workflow.list.actions.delete')}
						</DropdownMenuItem>
					</DropdownMenuContent>
				</DropdownMenu>
			</ItemActions>
		</Item>
	);
}

export function WorkflowPage() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const { workflows, loading, error } = useWorkflows();
	const createWorkflow = useCreateWorkflow();
	const deleteWorkflow = useDeleteWorkflow();
	const [deleteTarget, setDeleteTarget] = React.useState<WorkflowRecord | null>(null);
	// The run target outlives the dialog's close so the exit animation
	// keeps showing the preset form instead of flashing the workflow
	// Select in; the next Run click replaces the id.
	const [runOpen, setRunOpen] = React.useState(false);
	const [runWorkflowId, setRunWorkflowId] = React.useState<string | undefined>(undefined);
	const [duplicatingId, setDuplicatingId] = React.useState<string | null>(null);

	// No id means "new workflow" — the builder prompts for a name.
	const openBuilder = (workflowId?: string) =>
		navigate(workflowId ? `/workflow/builder/${workflowId}` : '/workflow/builder/');

	const handleDuplicate = async (workflow: WorkflowRecord) => {
		setDuplicatingId(workflow.id);
		try {
			// Fetch the canonical content so the copy matches what the
			// builder would load, not just the list projection.
			const detail = await getWorkflow(workflow.id);
			await createWorkflow.mutateAsync({
				...detail.data,
				name: `${detail.data.name}${t('workflow.list.copySuffix')}`,
				version: undefined,
			});
			toast.success(t('workflow.list.duplicated'));
		} catch {
			// The API client already toasted the failure detail.
		} finally {
			setDuplicatingId(null);
		}
	};

	const handleRetry = () =>
		queryClient.invalidateQueries({ queryKey: WORKFLOWS_KEY });

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div>
						<div className="text-2xl font-semibold">{t('common.workflow')}</div>
						<div className="mt-1 text-sm text-muted-foreground">
							{t('workflow.subtitle')}
						</div>
					</div>
					<Button size="sm" onClick={() => openBuilder()}>
						<Plus />
						{t('workflow.list.new')}
					</Button>
				</div>
				<Separator />

				<div className="flex-1 overflow-y-auto px-6 py-6">
					{loading ? (
						<div className="space-y-3">
							{Array.from({ length: 3 }).map((_, i) => (
								<Skeleton key={i} className="h-16 rounded-lg" />
							))}
						</div>
					) : error ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<Workflow />
								</EmptyMedia>
								<EmptyTitle>{t('workflow.list.loadFailed')}</EmptyTitle>
								<EmptyDescription>{error.message}</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={handleRetry}>
									{t('workflow.list.retry')}
								</Button>
							</EmptyContent>
						</Empty>
					) : workflows.length === 0 ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<Workflow />
								</EmptyMedia>
								<EmptyTitle>{t('workflow.list.emptyTitle')}</EmptyTitle>
								<EmptyDescription>
									{t('workflow.list.emptyDescription')}
								</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={() => openBuilder()}>
									<Plus />
									{t('workflow.list.create')}
								</Button>
							</EmptyContent>
						</Empty>
					) : (
						<section>
							<div className="mb-4 flex items-center gap-2">
								<span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
									{t('workflow.list.label')}
								</span>
								<span className="rounded bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
									{workflows.length}
								</span>
							</div>
							<div className="space-y-3">
								{workflows.map((workflow) => (
									<WorkflowCard
										key={workflow.id}
										workflow={workflow}
										duplicating={duplicatingId === workflow.id}
										onOpen={() => openBuilder(workflow.id)}
										onRun={() => {
											setRunWorkflowId(workflow.id);
											setRunOpen(true);
										}}
										onDuplicate={() => void handleDuplicate(workflow)}
										onViewRuns={() =>
											navigate(`/workflow/runs?workflowId=${workflow.id}`)
										}
										onDelete={() => setDeleteTarget(workflow)}
									/>
								))}
							</div>
						</section>
					)}
				</div>
			</main>

			<DeleteDialog
				open={deleteTarget !== null}
				onOpenChange={(open) => !open && setDeleteTarget(null)}
				title={t('common.deleteTitle', {
					entity: t('workflow.list.deleteEntity'),
					name: deleteTarget?.data.name ?? '',
				})}
				description={t('common.deleteDescription')}
				onConfirm={async () => {
					if (!deleteTarget) return;
					const id = deleteTarget.id;
					await deleteWorkflow.mutateAsync(id);
					setDeleteTarget(null);
				}}
			/>

			<RunWorkflowDialog
				workflowId={runWorkflowId}
				open={runOpen}
				onOpenChange={setRunOpen}
			/>
		</div>
	);
}
