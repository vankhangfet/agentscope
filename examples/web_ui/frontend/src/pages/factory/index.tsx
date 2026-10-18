import { useQueryClient } from '@tanstack/react-query';
import { format } from 'date-fns';
import { Factory, Plus } from 'lucide-react';
import * as React from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';

import { FactoryModeBadge } from './components/FactoryModeBadge';
import { FactoryPhaseBadge } from './components/FactoryPhaseBadge';
import { FactoryStatusBadge } from './components/FactoryStatusBadge';
import { FACTORY_TERMINAL_STATUSES } from '@/api';
import type { FactoryStatus, FactoryTicketView } from '@/api';
import { CreateFactoryTicketDialog } from '@/components/dialog/CreateFactoryTicketDialog';
import { Button } from '@/components/ui/button';
import {
	Empty,
	EmptyContent,
	EmptyDescription,
	EmptyHeader,
	EmptyMedia,
	EmptyTitle,
} from '@/components/ui/empty';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
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
import { FACTORY_TICKETS_KEY, useFactoryTickets } from '@/hooks/useFactoryTickets';
import { useTranslation } from '@/i18n/useI18n';

/** The filter's "no filter" value — radix Select rejects empty strings. */
const ALL_STATUSES = 'ALL';

/** Every ticket status, in filter order. */
const STATUSES: FactoryStatus[] = [
	'PENDING',
	'RUNNING',
	'WAITING_HUMAN',
	'COMPLETED',
	'FAILED',
	'CANCELLED',
];

/**
 * One ticket row: title, phase chip, status chip, mode badge, updated
 * time. Anywhere on the row opens the live ticket view.
 */
function TicketRow({ ticket, onOpen }: { ticket: FactoryTicketView; onOpen: () => void }) {
	return (
		<TableRow className="cursor-pointer" onClick={onOpen}>
			<TableCell className="max-w-[320px] px-4 py-2.5">
				<Link
					to={`/factory/${ticket.ticket_id}`}
					className="block truncate text-[12.5px] font-medium text-foreground hover:underline"
					title={ticket.title}
					// The row click already routes; keep the link from
					// double-firing the same navigation.
					onClick={(e) => e.stopPropagation()}
				>
					{ticket.title}
				</Link>
			</TableCell>
			<TableCell className="px-4 py-2.5">
				<FactoryPhaseBadge phase={ticket.phase} />
			</TableCell>
			<TableCell className="px-4 py-2.5">
				<FactoryStatusBadge status={ticket.status} />
			</TableCell>
			<TableCell className="px-4 py-2.5">
				<FactoryModeBadge mode={ticket.mode} />
			</TableCell>
			<TableCell className="px-4 py-2.5 font-mono text-[11px] text-text-secondary">
				{format(new Date(ticket.updated_at), 'yyyy-MM-dd HH:mm')}
			</TableCell>
		</TableRow>
	);
}

/**
 * The factory ticket list: every ticket the caller opened, newest
 * first. Polls every 5s while any visible ticket is still live; rows
 * open the ticket's live view. The discussions list's structure with
 * the workflow-runs page's status filter.
 */
export function FactoryPage() {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const queryClient = useQueryClient();
	const [creating, setCreating] = React.useState(false);
	const [searchParams, setSearchParams] = useSearchParams();

	const status = (searchParams.get('status') as FactoryStatus | null) ?? undefined;
	const filters = { ...(status != null && { status }) };

	const setStatusFilter = (next: string) => {
		setSearchParams((params) => {
			if (next === ALL_STATUSES) params.delete('status');
			else params.set('status', next);
			return params;
		});
	};

	// Poll only while something visible is live: the effect flips the
	// flag on every fetch, and the final fetch that sees all-terminal
	// rows is itself the last poll.
	const [poll, setPoll] = React.useState(false);
	const { tickets, total, loading, error } = useFactoryTickets(filters, {
		refetchInterval: poll ? 5000 : false,
	});
	React.useEffect(() => {
		setPoll(tickets.some((ticket) => !FACTORY_TERMINAL_STATUSES.includes(ticket.status)));
	}, [tickets]);

	const handleRetry = () => queryClient.invalidateQueries({ queryKey: FACTORY_TICKETS_KEY });

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<div className="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
					<div>
						<div className="text-2xl font-semibold">{t('factory.list.title')}</div>
						<div className="mt-1 text-sm text-muted-foreground">
							{t('factory.subtitle')}
						</div>
					</div>
					<div className="flex shrink-0 items-center gap-2">
						<Select value={status ?? ALL_STATUSES} onValueChange={setStatusFilter}>
							<SelectTrigger className="h-8 w-44">
								<SelectValue />
							</SelectTrigger>
							<SelectContent>
								<SelectItem value={ALL_STATUSES}>
									{t('factory.list.allStatuses')}
								</SelectItem>
								{STATUSES.map((value) => (
									<SelectItem key={value} value={value}>
										{t(`factory.status.${value}`)}
									</SelectItem>
								))}
							</SelectContent>
						</Select>
						<Button size="sm" onClick={() => setCreating(true)}>
							<Plus />
							{t('factory.list.new')}
						</Button>
					</div>
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
									<Factory />
								</EmptyMedia>
								<EmptyTitle>{t('factory.list.loadFailed')}</EmptyTitle>
								<EmptyDescription>{error.message}</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={handleRetry}>
									{t('factory.list.retry')}
								</Button>
							</EmptyContent>
						</Empty>
					) : tickets.length === 0 ? (
						<Empty className="border-none py-10">
							<EmptyHeader>
								<EmptyMedia variant="icon">
									<Factory />
								</EmptyMedia>
								<EmptyTitle>{t('factory.list.emptyTitle')}</EmptyTitle>
								<EmptyDescription>
									{t('factory.list.emptyDescription')}
								</EmptyDescription>
							</EmptyHeader>
							<EmptyContent>
								<Button variant="outline" size="sm" onClick={() => setCreating(true)}>
									<Plus />
									{t('factory.list.new')}
								</Button>
							</EmptyContent>
						</Empty>
					) : (
						<section>
							<div className="mb-4 flex items-center gap-2">
								<span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
									{t('factory.list.label')}
								</span>
								<span className="rounded bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
									{t('factory.list.count', { count: total })}
								</span>
							</div>
							<div className="overflow-x-auto rounded-[16px] border border-border">
								<Table className="min-w-[640px]">
									<TableHeader>
										<TableRow className="border-border hover:bg-transparent">
											<TableHead className="px-4">
												{t('factory.list.table.title')}
											</TableHead>
											<TableHead className="px-4">
												{t('factory.list.table.phase')}
											</TableHead>
											<TableHead className="px-4">
												{t('factory.list.table.status')}
											</TableHead>
											<TableHead className="px-4">
												{t('factory.list.table.mode')}
											</TableHead>
											<TableHead className="px-4">
												{t('factory.list.table.updated')}
											</TableHead>
										</TableRow>
									</TableHeader>
									<TableBody>
										{tickets.map((ticket) => (
											<TicketRow
												key={ticket.ticket_id}
												ticket={ticket}
												onOpen={() => navigate(`/factory/${ticket.ticket_id}`)}
											/>
										))}
									</TableBody>
								</Table>
							</div>
						</section>
					)}
				</div>
			</main>

			<CreateFactoryTicketDialog
				open={creating}
				onOpenChange={setCreating}
				onCreated={(ticketId) => navigate(`/factory/${ticketId}`)}
			/>
		</div>
	);
}
