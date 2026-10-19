import { ChevronDown, Plus } from 'lucide-react';
import * as React from 'react';
import { toast } from 'sonner';

import type { ChatModelConfig, FactoryMode, FactoryRoleGroupView, FactoryRoleView } from '@/api';
import { LlmSelect } from '@/components/select/LlmSelect';
import { Button } from '@/components/ui/button';
import {
	Collapsible,
	CollapsibleContent,
	CollapsibleTrigger,
} from '@/components/ui/collapsible';
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
import { Textarea } from '@/components/ui/textarea';
import { useCreateFactoryTicket, useFactoryRoster } from '@/hooks/useFactoryTickets';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	/** Called with the new ticket's id once a create succeeds. */
	onCreated?: (ticketId: string) => void;
}

/**
 * One roster group as the dialog shows it: the group's title with a
 * count chip, then its roles as chips — clicking a chip unfolds that
 * role's stance underneath. Stances come straight from the catalog;
 * only the chrome around them is translated.
 */
function RosterGroup({
	group,
	expanded,
	onToggle,
}: {
	group: FactoryRoleGroupView;
	expanded: string | null;
	onToggle: (key: string) => void;
}) {
	return (
		<div className="space-y-1.5">
			<div className="flex items-center gap-2">
				<span className="text-xs font-semibold">{group.title}</span>
				<span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
					{group.roles.length}
				</span>
			</div>
			<div className="flex flex-wrap gap-1.5">
				{group.roles.map((role) => {
					const key = `${group.phase}:${role.name}`;
					const open = expanded === key;
					return (
						<button
							key={key}
							type="button"
							aria-pressed={open}
							className={cn(
								'rounded-full border px-2.5 py-0.5 text-xs transition-colors',
								open
									? 'border-primary bg-primary/10 text-primary'
									: 'border-border text-muted-foreground hover:text-foreground',
							)}
							onClick={() => onToggle(key)}
						>
							{role.title}
						</button>
					);
				})}
			</div>
			{group.roles.map((role) => {
				const key = `${group.phase}:${role.name}`;
				return expanded === key ? (
					<p
						key={key}
						className="rounded bg-muted/50 p-2 text-xs leading-relaxed break-words whitespace-pre-wrap text-muted-foreground"
					>
						{role.stance}
					</p>
				) : null;
			})}
		</div>
	);
}

/**
 * The "Agents involved" section: the whole roster — every group's
 * roles, with the judge noted under the two discussion groups — so
 * the user knows the team before opening the ticket.
 */
function AgentsInvolvedSection({
	groups,
	judge,
}: {
	groups: FactoryRoleGroupView[];
	judge: FactoryRoleView | null;
}) {
	const { t } = useTranslation();
	const [open, setOpen] = React.useState(false);
	const [expanded, setExpanded] = React.useState<string | null>(null);

	const toggle = (key: string) =>
		setExpanded((current) => (current === key ? null : key));

	return (
		<Collapsible open={open} onOpenChange={setOpen}>
			<CollapsibleTrigger className="flex w-full items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground">
				<ChevronDown className="size-4 transition-transform data-[state=open]:rotate-180" />
				{t('factory.roster.title')}
			</CollapsibleTrigger>
			<CollapsibleContent className="mt-3 space-y-3">
				{groups.map((group) => (
					<React.Fragment key={group.phase}>
						<RosterGroup group={group} expanded={expanded} onToggle={toggle} />
						{/* The judge arbitrates both debates, so it is noted
						    right after the second discussion group. */}
						{group.phase === 'ARCHITECTURE' && judge != null && (
							<p className="text-xs leading-relaxed break-words text-muted-foreground">
								{t('factory.roster.judgeShared', { title: judge.title })} —{' '}
								{judge.stance}
							</p>
						)}
					</React.Fragment>
				))}
			</CollapsibleContent>
		</Collapsible>
	);
}

/**
 * The "New Ticket" dialog: the problem in one line, the fuller
 * description the factory works from, the model every role shares and
 * the gate mode. Validation mirrors the backend battery — a non-blank
 * title and a picked model keep the submit button enabled.
 */
export function CreateFactoryTicketDialog({ open, onOpenChange, onCreated }: Props) {
	const { t } = useTranslation();
	const create = useCreateFactoryTicket();
	const roster = useFactoryRoster();

	const [title, setTitle] = React.useState('');
	const [description, setDescription] = React.useState('');
	const [model, setModel] = React.useState<ChatModelConfig | null>(null);
	const [mode, setMode] = React.useState<FactoryMode>('COLLABORATIVE');

	// On close, reset to blank like the other create dialogs so a stale
	// prefill never bleeds into the next open.
	React.useEffect(() => {
		if (open) return;
		setTitle('');
		setDescription('');
		setModel(null);
		setMode('COLLABORATIVE');
	}, [open]);

	const valid = title.trim() !== '' && model !== null;

	const handleSubmit = async () => {
		if (!valid || create.isPending) return;
		try {
			const res = await create.mutateAsync({
				title: title.trim(),
				description: description.trim(),
				chat_model_config: model as ChatModelConfig,
				mode,
			});
			toast.success(t('factory.create.created'));
			onOpenChange(false);
			onCreated?.(res.ticket_id);
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-[520px]">
				<DialogHeader>
					<DialogTitle>{t('factory.create.title')}</DialogTitle>
					<DialogDescription>{t('factory.create.description')}</DialogDescription>
				</DialogHeader>

				<FieldGroup>
					<Field>
						<FieldLabel>{t('factory.create.titleLabel')}</FieldLabel>
						<Input
							value={title}
							placeholder={t('factory.create.titlePlaceholder')}
							disabled={create.isPending}
							onChange={(e) => setTitle(e.target.value)}
						/>
					</Field>

					<Field>
						<FieldLabel>{t('factory.create.descriptionLabel')}</FieldLabel>
						<Textarea
							rows={4}
							value={description}
							placeholder={t('factory.create.descriptionPlaceholder')}
							disabled={create.isPending}
							onChange={(e) => setDescription(e.target.value)}
						/>
					</Field>

					<Field>
						<FieldLabel>{t('factory.create.modelLabel')}</FieldLabel>
						<LlmSelect
							value={model}
							placeholder={t('factory.create.modelPlaceholder')}
							disabled={create.isPending}
							onChange={setModel}
						/>
					</Field>

					<Field>
						<FieldLabel>{t('factory.create.modeLabel')}</FieldLabel>
						<div className="grid grid-cols-2 gap-2" role="group" aria-label={t('factory.create.modeLabel')}>
							{(['COLLABORATIVE', 'DELEGATED'] as const).map((value) => (
								<Button
									key={value}
									type="button"
									variant={mode === value ? 'default' : 'outline'}
									aria-pressed={mode === value}
									disabled={create.isPending}
									onClick={() => setMode(value)}
								>
									{t(`factory.mode.${value}`)}
								</Button>
							))}
						</div>
						<p className="text-muted-foreground text-xs">
							{mode === 'COLLABORATIVE'
								? t('factory.create.modeCollaborativeHint')
								: t('factory.create.modeDelegatedHint')}
						</p>
					</Field>

					{/* The team preview is supplementary: it stays hidden
					    while the roster loads and degrades to a muted note
					    when it cannot load — the ticket itself is fine. */}
					{!roster.loading &&
						(roster.error ? (
							<p className="text-xs text-muted-foreground">
								{t('factory.roster.loadFailed')}
							</p>
						) : (
							<Field>
								<AgentsInvolvedSection groups={roster.groups} judge={roster.judge} />
							</Field>
						))}
				</FieldGroup>

				<DialogFooter>
					<Button variant="ghost" onClick={() => onOpenChange(false)} disabled={create.isPending}>
						{t('common.cancel')}
					</Button>
					<Button disabled={!valid || create.isPending} onClick={() => void handleSubmit()}>
						{create.isPending ? <Spinner className="size-3.5" /> : <Plus />}
						{create.isPending ? t('common.creating') : t('factory.create.submit')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
