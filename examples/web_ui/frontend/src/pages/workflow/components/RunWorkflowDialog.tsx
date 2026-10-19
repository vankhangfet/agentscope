import { Play } from 'lucide-react';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';

import type { ChatModelConfig } from '@/api';
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
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useStartRun } from '@/hooks/useWorkflowRuns';
import { useWorkflows } from '@/hooks/useWorkflows';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	/**
	 * The workflow to run. Absent — launched from a context with no
	 * workflow at hand, like the runs page's empty state — the dialog
	 * shows a workflow Select fed by the workflows list instead.
	 */
	workflowId?: string;
	open: boolean;
	onOpenChange: (open: boolean) => void;
}

/** Parse run-input text; `ok: false` when it isn't valid JSON. */
type ParseResult = { ok: true; value: unknown } | { ok: false };

function parseJson(text: string): ParseResult {
	try {
		return { ok: true, value: JSON.parse(text) };
	} catch {
		return { ok: false };
	}
}

/**
 * The "Run workflow" dialog: the run's JSON input, an optional run-level
 * default model, and — when no workflow was handed in — a workflow
 * Select. A successful start navigates to the run's live detail view;
 * failures keep the dialog open (the API client already toasted them).
 */
export function RunWorkflowDialog({ workflowId, open, onOpenChange }: Props) {
	const { t } = useTranslation();
	const navigate = useNavigate();
	const startRun = useStartRun();
	const { workflows } = useWorkflows();

	const [input, setInput] = React.useState('{}');
	const [model, setModel] = React.useState<ChatModelConfig | null>(null);
	const [selectedId, setSelectedId] = React.useState('');

	// On close, reset to blank like the other create dialogs so a stale
	// prefill never bleeds into the next open.
	React.useEffect(() => {
		if (open) return;
		setInput('{}');
		setModel(null);
		setSelectedId('');
	}, [open]);

	// Live input validation: an empty textarea means `{}`; anything else
	// must parse to a JSON object (the run's blackboard root).
	const trimmed = input.trim();
	const parsed = trimmed === '' ? { ok: true, value: {} } : parseJson(trimmed);
	const inputObject =
		parsed.ok && typeof parsed.value === 'object' && !Array.isArray(parsed.value)
			? (parsed.value as Record<string, unknown>)
			: null;
	const inputError =
		!parsed.ok
			? t('workflow.runDialog.inputInvalid')
			: inputObject === null
				? t('workflow.runDialog.inputNotObject')
				: null;

	const effectiveId = workflowId ?? selectedId;
	const valid = inputError === null && effectiveId !== '';

	const handleSubmit = async () => {
		if (!valid || startRun.isPending) return;
		try {
			const res = await startRun.mutateAsync({
				workflowId: effectiveId,
				body: {
					input: inputObject ?? {},
					// Omit the field entirely when unset: an empty
					// placeholder would fail the backend's model checks.
					...(model !== null && { chat_model_config: model }),
				},
			});
			onOpenChange(false);
			navigate(`/workflow/runs/${res.run_id}`);
		} catch {
			// The API client already toasted the failure detail; the
			// dialog stays open so the user can adjust and retry.
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="sm:max-w-md">
				<DialogHeader>
					<DialogTitle>{t('workflow.runDialog.title')}</DialogTitle>
					<DialogDescription>{t('workflow.runDialog.description')}</DialogDescription>
				</DialogHeader>

				<FieldGroup>
					{workflowId === undefined && (
						<Field>
							<FieldLabel>{t('workflow.runDialog.workflowLabel')}</FieldLabel>
							{workflows.length > 0 ? (
								<Select value={selectedId} onValueChange={setSelectedId}>
									<SelectTrigger>
										<SelectValue
											placeholder={t('workflow.runDialog.workflowPlaceholder')}
										/>
									</SelectTrigger>
									<SelectContent>
										{workflows.map((w) => (
											<SelectItem key={w.id} value={w.id}>
												{w.data.name}
											</SelectItem>
										))}
									</SelectContent>
								</Select>
							) : (
								<p className="text-xs text-muted-foreground">
									{t('workflow.runDialog.noWorkflows')}
								</p>
							)}
						</Field>
					)}

					<Field>
						<FieldLabel>{t('workflow.runDialog.inputLabel')}</FieldLabel>
						<Textarea
							rows={4}
							className="font-mono text-xs"
							spellCheck={false}
							placeholder="{}"
							aria-invalid={inputError !== null}
							disabled={startRun.isPending}
							value={input}
							onChange={(e) => setInput(e.target.value)}
						/>
						{inputError !== null ? (
							<FieldError>{inputError}</FieldError>
						) : (
							<p className="text-xs text-muted-foreground">
								{t('workflow.runDialog.inputHint')}
							</p>
						)}
					</Field>

					<Field>
						<FieldLabel>{t('workflow.runDialog.modelLabel')}</FieldLabel>
						<LlmSelect
							className="w-full"
							allowClear
							value={model}
							placeholder={t('workflow.runDialog.modelPlaceholder')}
							disabled={startRun.isPending}
							onChange={setModel}
						/>
						<p className="text-xs text-muted-foreground">
							{t('workflow.runDialog.modelHint')}
						</p>
					</Field>
				</FieldGroup>

				<DialogFooter>
					<Button
						variant="ghost"
						onClick={() => onOpenChange(false)}
						disabled={startRun.isPending}
					>
						{t('common.cancel')}
					</Button>
					<Button
						disabled={!valid || startRun.isPending}
						onClick={() => void handleSubmit()}
					>
						{startRun.isPending ? (
							<Spinner className="size-3.5" />
						) : (
							<Play />
						)}
						{startRun.isPending
							? t('workflow.runDialog.submitting')
							: t('workflow.runDialog.submit')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
