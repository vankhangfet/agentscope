import { useQueryClient } from '@tanstack/react-query';
import { format } from 'date-fns';
import { Ellipsis, MessagesSquare, Plus, Trash2 } from 'lucide-react';
import * as React from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { DiscussionStatusBadge } from './components/DiscussionStatusBadge';
import { DISCUSSION_TERMINAL_STATUSES } from '@/api';
import type { Discussion } from '@/api';
import { CreateDiscussionDialog } from '@/components/dialog/CreateDiscussionDialog';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
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
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import {
	Table,
	TableBody,
	TableCell,
	TableHead,
	TableHeader,
	TableRow,
} from '@/components/ui/table';
import {
	DISCUSSIONS_KEY,
	useDeleteDiscussion,
	useDiscussions,
} from '@/hooks/useDiscussions';
import { useTranslation } from '@/i18n/useI18n';

/**
 * One discussion row: topic, status chip, debater count, rounds against
 * the ceiling, updated time. Anywhere on the row opens the live view;
 * the trailing "…" menu holds the row's destructive actions.
 */
function DiscussionRow({
	discussion,
	onOpen,
	onDelete,
}: {
	discussion: Discussion;
	onOpen: () => void;
	onDelete: () => void;
}) {
	const { t } = useTranslation();
	const { data } = discussion;
	return (
		<TableRow className="cursor-pointer" onClick={onOpen}>
			<TableCell className="max-w-[320px] px-4 py-2.5">
				<Link
					to={`/discussion/${discussion.id}`}
					className="block truncate text-[12.5px] font-medium text-foreground hover:underline"
					title={data.topic}
					// The row click already routes; keep the link from
					// double-firing the same navigation.
					onClick={(e) => e.stopPropagation()}
				>
					{data.topic}
				</Link>
			</TableCell>
			<TableCell className="px-4 py-2.5">
				<DiscussionStatusBadge status={data.status} />
			</TableCell>
			<TableCell className="px-4 py-2.5 text-[12px] text-text-secondary">
				{t('discussion.list.debaterCount', { count: data.debaters.length })}
			</TableCell>
			<TableCell className="px-4 py-2.5 font-mono text-[11px] text-text-secondary">
				{data.rounds.length} / {data.max_rounds}
			</TableCell>
			<TableCell className="px-4 py-2.5 font-mono text-[11px] text-text-secondary">
				{format(new Date(discussion.updated_at), 'yyyy-MM-dd HH:mm')}
			</TableCell>
			<TableCell className="px-4 py-2.5">
				<DropdownMenu>
					<DropdownMenuTrigger asChild>
						<Button
							variant="ghost"
							size="icon-sm"
							className="text-text-tertiary hover:text-foreground"
							aria-label={t('discussion.list.actions.menu')}
							// The row click already routes; keep the
							// menu from firing the same navigation.
							onClick={(e) => e.stopPropagation()}
						>
							<Ellipsis />
						</Button>
					</DropdownMenuTrigger>
					<DropdownMenuContent align="end">
						<DropdownMenuItem variant="destructive" onClick={onDelete}>
							<Trash2 />
							{t('common.delete')}
						</DropdownMenuItem>
					</DropdownMenuContent>
				</DropdownMenu>
			</TableCell>
		</TableRow>
	);
}

/**
 * The discussions list: every debate the caller started, newest first.
 * Polls every 5s while any visible discussion is still live; rows open
 * the live debate view. The workflow-runs page's structure.
 */
export function DiscussionPage() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const [creating, setCreating] = React.useState(false);
	const [deleteTarget, setDeleteTarget] = React.useState<Discussion | null>(null);
	const removeDiscussion = useDeleteDiscussion();

	// Poll only while something visible is live: the effect flips the
	// flag on every fetch, and the final fetch that sees all-terminal
	// rows is itself the last poll.
	const [poll, setPoll] = React.useState(false);
	const { discussions, total, loading, error } = useDiscussions(undefined, {
		refetchInterval: poll ? 5000 : false,
	});
	React.useEffect(() => {
		setPoll(
			discussions.some((d) => !DISCUSSION_TERMINAL_STATUSES.includes(d.data.status)),
		);
	}, [discussions]);

	const handleRetry = () => queryClient.invalidateQueries({ queryKey: DISCUSSIONS_KEY });

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div>
						<div className="text-2xl font-semibold">{t('discussion.list.title')}</div>
						<div className="mt-1 text-sm text-muted-foreground">
							{t('discussion.subtitle')}
						</div>
					</div>
					<Button size="sm" onClick={() => setCreating(true)}>
						<Plus />
						{t('discussion.list.new')}
					</Button>
				</div>
				<Separator />

				<div className="flex-1 overflow-y-auto px-6 py-6">
					{loading ? (
						<div className="space-y-3">
							{Array.from({ length: 5 }).map((_, i) => (
								<Skeleton key={i} className="h-12 rounded-lg" />
							))}
						</div>
					) : error ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<MessagesSquare />
								</EmptyMedia>
								<EmptyTitle>{t('discussion.list.loadFailed')}</EmptyTitle>
								<EmptyDescription>{error.message}</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={handleRetry}>
									{t('discussion.list.retry')}
								</Button>
							</EmptyContent>
						</Empty>
					) : discussions.length === 0 ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<MessagesSquare />
								</EmptyMedia>
								<EmptyTitle>{t('discussion.list.emptyTitle')}</EmptyTitle>
								<EmptyDescription>
									{t('discussion.list.emptyDescription')}
								</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={() => setCreating(true)}>
									<Plus />
									{t('discussion.list.new')}
								</Button>
							</EmptyContent>
						</Empty>
					) : (
						<section>
							<div className="mb-4 flex items-center gap-2">
								<span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
									{t('discussion.list.label')}
								</span>
								<span className="rounded bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
									{t('discussion.list.count', { count: total })}
								</span>
							</div>
							<div className="overflow-x-auto rounded-[16px] border border-border">
								<Table className="min-w-[640px]">
									<TableHeader>
										<TableRow className="border-border hover:bg-transparent">
											<TableHead className="px-4">
												{t('discussion.list.table.topic')}
											</TableHead>
											<TableHead className="px-4">
												{t('discussion.list.table.status')}
											</TableHead>
											<TableHead className="px-4">
												{t('discussion.list.table.debaters')}
											</TableHead>
											<TableHead className="px-4">
												{t('discussion.list.table.rounds')}
											</TableHead>
											<TableHead className="px-4">
												{t('discussion.list.table.updated')}
											</TableHead>
											{/* The row actions column: the "…" menu. */}
											<TableHead className="w-10 px-4" />
										</TableRow>
									</TableHeader>
									<TableBody>
										{discussions.map((discussion) => (
											<DiscussionRow
												key={discussion.id}
												discussion={discussion}
												onOpen={() => navigate(`/discussion/${discussion.id}`)}
												onDelete={() => setDeleteTarget(discussion)}
											/>
										))}
									</TableBody>
								</Table>
							</div>
						</section>
					)}
				</div>
			</main>

			<CreateDiscussionDialog open={creating} onOpenChange={setCreating} />

			<DeleteDialog
				open={deleteTarget !== null}
				onOpenChange={(open) => !open && setDeleteTarget(null)}
				title={t('common.deleteTitle', {
					entity: t('discussion.deleteEntity'),
					name: deleteTarget?.data.topic ?? '',
				})}
				description={t('common.deleteDescription')}
				onConfirm={async () => {
					if (!deleteTarget) return;
					const target = deleteTarget;
					try {
						await removeDiscussion.mutateAsync(target.id);
						setDeleteTarget(null);
					} catch {
						// The API client already toasted the failure
						// detail; the dialog stays open for a retry.
					}
				}}
			/>
		</div>
	);
}
