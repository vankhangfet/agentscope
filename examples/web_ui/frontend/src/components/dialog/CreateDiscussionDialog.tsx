import { Plus, Trash2 } from 'lucide-react';
import * as React from 'react';
import { toast } from 'sonner';

import type { ChatModelConfig, CreateDiscussionRequest, DebaterConfig } from '@/api';
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
import { Separator } from '@/components/ui/separator';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useCreateDiscussion } from '@/hooks/useDiscussions';
import { useTranslation } from '@/i18n/useI18n';

/** The backend's structural bounds, mirrored client-side. */
const MIN_DEBATERS = 1;
const MAX_DEBATERS = 6;
const MIN_ROUNDS = 1;
const MAX_ROUNDS = 10;
/** How many empty rows a fresh form opens with — a debate needs sides. */
const INITIAL_DEBATERS = 2;

/** One editable debater row, before it becomes a `DebaterConfig`. */
interface DebaterDraft {
	/** React list key, stable across edits. */
	key: number;
	name: string;
	systemPrompt: string;
	model: ChatModelConfig | null;
}

/** Two blank debater rows plus the next row key. */
function freshForm(): { debaters: DebaterDraft[]; nextKey: number } {
	return {
		debaters: Array.from({ length: INITIAL_DEBATERS }, (_, i) => ({
			key: i + 1,
			name: '',
			systemPrompt: '',
			model: null,
		})),
		nextKey: INITIAL_DEBATERS + 1,
	};
}

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
}

/**
 * The "New Discussion" dialog: a topic, 1-6 debater rows (name, stance,
 * model), an optional judge and the round ceiling. Validation mirrors
 * the backend battery — blank topic, missing names/models or repeated
 * names keep the create button disabled; the count and round bounds are
 * enforced by the controls themselves.
 */
export function CreateDiscussionDialog({ open, onOpenChange }: Props) {
	const { t } = useTranslation();
	const create = useCreateDiscussion();

	const [topic, setTopic] = React.useState('');
	const [debaters, setDebaters] = React.useState<DebaterDraft[]>(freshForm().debaters);
	const nextKey = React.useRef(freshForm().nextKey);
	const [judgeModel, setJudgeModel] = React.useState<ChatModelConfig | null>(null);
	const [judgePrompt, setJudgePrompt] = React.useState('');
	const [maxRounds, setMaxRounds] = React.useState(3);

	// A closed dialog reopens blank, like the other create dialogs.
	React.useEffect(() => {
		if (open) return;
		setTopic('');
		const fresh = freshForm();
		setDebaters(fresh.debaters);
		nextKey.current = fresh.nextKey;
		setJudgeModel(null);
		setJudgePrompt('');
		setMaxRounds(3);
	}, [open]);

	const addDebater = () => {
		// Mint the key outside the updater, keeping the updater pure.
		const key = nextKey.current;
		nextKey.current += 1;
		setDebaters((rows) =>
			rows.length >= MAX_DEBATERS
				? rows
				: [...rows, { key, name: '', systemPrompt: '', model: null }],
		);
	};

	const removeDebater = (key: number) =>
		setDebaters((rows) => (rows.length <= MIN_DEBATERS ? rows : rows.filter((r) => r.key !== key)));

	const patchDebater = (key: number, patch: Partial<DebaterDraft>) =>
		setDebaters((rows) => rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));

	// The facade's battery, client-side: a non-blank topic and rows that
	// each carry a unique non-blank name plus a model. The 1-6 count and
	// the 1-10 round ceiling are already guarded by the controls.
	const trimmedNames = debaters.map((row) => row.name.trim());
	const hasDuplicates = trimmedNames.some(
		(name, i) => name !== '' && trimmedNames.indexOf(name) !== i,
	);
	const valid =
		topic.trim() !== '' &&
		debaters.every((row) => row.name.trim() !== '' && row.model !== null) &&
		!hasDuplicates;

	const handleSubmit = async () => {
		if (!valid || create.isPending) return;
		const payload: CreateDiscussionRequest = {
			topic: topic.trim(),
			debaters: debaters.map(
				(row): DebaterConfig => ({
					name: row.name.trim(),
					system_prompt: row.systemPrompt.trim(),
					chat_model_config: row.model as ChatModelConfig,
				}),
			),
			max_rounds: maxRounds,
		};
		// A judge is sent when any judge field was touched. No judge model
		// picked: run the evaluator on the first debater's model — the
		// backend's own default for a missing judge.
		if (judgeModel != null || judgePrompt.trim() !== '') {
			payload.judge = {
				chat_model_config: judgeModel ?? (debaters[0].model as ChatModelConfig),
				...(judgePrompt.trim() !== '' && { system_prompt: judgePrompt.trim() }),
			};
		}
		try {
			await create.mutateAsync(payload);
			toast.success(t('discussion.create.created'));
			onOpenChange(false);
		} catch {
			// The API client already toasted the failure detail.
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-[560px]">
				<DialogHeader>
					<DialogTitle>{t('discussion.create.title')}</DialogTitle>
					<DialogDescription>{t('discussion.create.description')}</DialogDescription>
				</DialogHeader>

				<FieldGroup>
					<Field>
						<FieldLabel>{t('discussion.create.topic')}</FieldLabel>
						<Textarea
							rows={2}
							value={topic}
							placeholder={t('discussion.create.topicPlaceholder')}
							disabled={create.isPending}
							onChange={(e) => setTopic(e.target.value)}
						/>
					</Field>

					<Field>
						<div className="flex items-center gap-2">
							<FieldLabel>{t('discussion.create.debaters')}</FieldLabel>
							<span className="rounded bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
								{debaters.length} / {MAX_DEBATERS}
							</span>
						</div>
						<div className="space-y-3">
							{debaters.map((row, i) => (
								<div
									key={row.key}
									className="space-y-2 rounded-lg border border-border p-3"
								>
									<div className="flex items-center justify-between gap-2">
										<span className="text-xs font-semibold text-muted-foreground">
											{t('discussion.create.debater', { index: i + 1 })}
										</span>
										<Button
											variant="ghost"
											size="icon-sm"
											className="text-text-tertiary hover:text-foreground"
											disabled={debaters.length <= MIN_DEBATERS || create.isPending}
											aria-label={t('discussion.create.removeDebater', { index: i + 1 })}
											onClick={() => removeDebater(row.key)}
										>
											<Trash2 />
										</Button>
									</div>
									<div className="grid gap-2 sm:grid-cols-2">
										<Input
											value={row.name}
											placeholder={t('discussion.create.namePlaceholder')}
											aria-invalid={
												row.name.trim() !== '' &&
												trimmedNames.filter((n) => n === row.name.trim()).length > 1
											}
											disabled={create.isPending}
											onChange={(e) => patchDebater(row.key, { name: e.target.value })}
										/>
										<LlmSelect
											value={row.model}
											placeholder={t('discussion.create.modelPlaceholder')}
											disabled={create.isPending}
											onChange={(model) => patchDebater(row.key, { model })}
										/>
									</div>
									<Textarea
										rows={2}
										value={row.systemPrompt}
										placeholder={t('discussion.create.stancePlaceholder')}
										disabled={create.isPending}
										onChange={(e) => patchDebater(row.key, { systemPrompt: e.target.value })}
									/>
								</div>
							))}
						</div>
						{hasDuplicates && (
							<p className="text-destructive text-sm">
								{t('discussion.create.errors.duplicateNames')}
							</p>
						)}
						<Button
							variant="outline"
							size="sm"
							disabled={debaters.length >= MAX_DEBATERS || create.isPending}
							onClick={addDebater}
						>
							<Plus />
							{t('discussion.create.addDebater')}
						</Button>
					</Field>

					<Separator />

					<Field>
						<FieldLabel>{t('discussion.create.judge')}</FieldLabel>
						<p className="text-muted-foreground text-xs">
							{t('discussion.create.judgeHint')}
						</p>
						<LlmSelect
							value={judgeModel}
							placeholder={t('discussion.create.judgeModelPlaceholder')}
							allowClear
							disabled={create.isPending}
							onChange={setJudgeModel}
						/>
						<Textarea
							rows={2}
							value={judgePrompt}
							placeholder={t('discussion.create.judgePromptPlaceholder')}
							disabled={create.isPending}
							onChange={(e) => setJudgePrompt(e.target.value)}
						/>
					</Field>

					<Separator />

					<Field>
						<FieldLabel>{t('discussion.create.maxRounds')}</FieldLabel>
						<div className="flex items-center gap-3">
							<Input
								type="number"
								min={MIN_ROUNDS}
								max={MAX_ROUNDS}
								value={maxRounds}
								className="w-20"
								disabled={create.isPending}
								onChange={(e) => {
									const parsed = e.target.valueAsNumber;
									if (Number.isFinite(parsed)) {
										setMaxRounds(
											Math.min(MAX_ROUNDS, Math.max(MIN_ROUNDS, Math.round(parsed))),
										);
									}
								}}
							/>
							<span className="text-muted-foreground text-xs">
								{t('discussion.create.maxRoundsHint')}
							</span>
						</div>
					</Field>
				</FieldGroup>

				<DialogFooter>
					<Button variant="ghost" onClick={() => onOpenChange(false)} disabled={create.isPending}>
						{t('common.cancel')}
					</Button>
					<Button disabled={!valid || create.isPending} onClick={() => void handleSubmit()}>
						{create.isPending ? <Spinner className="size-3.5" /> : <Plus />}
						{create.isPending ? t('common.creating') : t('discussion.create.submit')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
