import { Plus } from 'lucide-react';
import * as React from 'react';
import { toast } from 'sonner';

import type { ChatModelConfig, FactoryMode } from '@/api';
import { LlmSelect } from '@/components/select/LlmSelect';
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
import { Textarea } from '@/components/ui/textarea';
import { useCreateFactoryTicket } from '@/hooks/useFactoryTickets';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	/** Called with the new ticket's id once a create succeeds. */
	onCreated?: (ticketId: string) => void;
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
