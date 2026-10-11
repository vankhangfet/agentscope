import { CircleCheck, CircleX, FlaskConical, Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';

import { testCredential } from '@/api';
import type { CredentialView, ModelCard, TestCredentialResult } from '@/api';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	credential: CredentialView;
	/**
	 * Chat models the credential's provider advertises, when they are
	 * known — drives a select instead of a free-text model input. May
	 * be empty (still loading, or a provider that lists none).
	 */
	models: ModelCard[];
}

/**
 * "Test connection" dialog for one credential: picks a model (select
 * when the provider enumerates models, free text otherwise) plus
 * optional call parameters, then exercises the credential with a
 * single minimal model call via `POST /credential/{id}/test`.
 *
 * Provider, auth and network failures are data on that endpoint —
 * they land in the red result banner, not as a thrown error.
 */
export function TestCredentialDialog({ open, onOpenChange, credential, models }: Props) {
	const { t } = useTranslation();
	const [model, setModel] = useState('');
	const [parametersText, setParametersText] = useState('{}');
	const [testing, setTesting] = useState(false);
	const [result, setResult] = useState<TestCredentialResult | null>(null);
	const [requestError, setRequestError] = useState('');

	// Fresh form on every open; the default model follows whatever the
	// provider's list holds at that moment.
	useEffect(() => {
		if (!open) return;
		setModel(models[0]?.name ?? '');
		setParametersText('{}');
		setResult(null);
		setRequestError('');
		// `models` is deliberately not a dependency: it only re-runs on
		// open, so a slow model list doesn't clobber the user's pick.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [open, credential.id]);

	const parameters = parseParameters(parametersText);
	const canSubmit = !!model.trim() && parameters !== null && !testing;

	const handleTest = async () => {
		if (!canSubmit) return;
		setTesting(true);
		setResult(null);
		setRequestError('');
		try {
			// `parameters` is non-null whenever the button is enabled.
			const res = await testCredential(credential.id, {
				model: model.trim(),
				parameters: parameters as Record<string, unknown>,
			});
			setResult(res);
		} catch (e) {
			// Transport-level failure (server unreachable, 4xx/5xx) — the
			// endpoint itself reports provider failures as ok=false data.
			setRequestError(e instanceof Error ? e.message : String(e));
		} finally {
			setTesting(false);
		}
	};

	const name = (credential.data.name as string | undefined) ?? credential.id;

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="!w-[480px] !max-w-[480px]">
				<DialogHeader>
					<DialogTitle>{t('credential.test.title')}</DialogTitle>
					<DialogDescription>
						{t('credential.test.description', { name })}
					</DialogDescription>
				</DialogHeader>

				<FieldGroup>
					<Field>
						<FieldLabel>{t('credential.test.model')}</FieldLabel>
						{models.length > 0 ? (
							<Select value={model} onValueChange={setModel}>
								<SelectTrigger>
									<SelectValue placeholder={t('credential.test.modelPlaceholder')} />
								</SelectTrigger>
								<SelectContent>
									{models.map((m) => (
										<SelectItem key={m.name} value={m.name}>
											{m.label || m.name}
										</SelectItem>
									))}
								</SelectContent>
							</Select>
						) : (
							// Provider lists no models (or they are still
							// loading) — let the user type any model name.
							<Input
								value={model}
								onChange={(e) => setModel(e.target.value)}
								placeholder={t('credential.test.modelPlaceholder')}
							/>
						)}
					</Field>

					<Field data-invalid={parameters === null || undefined}>
						<FieldLabel>{t('credential.test.parameters')}</FieldLabel>
						<Textarea
							value={parametersText}
							onChange={(e) => setParametersText(e.target.value)}
							className="min-h-[72px] font-mono text-xs"
							spellCheck={false}
						/>
						{parameters === null ? (
							<FieldError>{t('credential.test.parametersInvalid')}</FieldError>
						) : (
							<FieldDescription>{t('credential.test.parametersHint')}</FieldDescription>
						)}
					</Field>

					{result && (
						<div
							className={
								result.ok
									? 'flex items-start gap-2 rounded-md bg-emerald-500/10 p-3 text-sm text-emerald-700 dark:text-emerald-400'
									: 'flex items-start gap-2 rounded-md bg-destructive/10 p-3 text-sm text-destructive'
							}
							role="status"
						>
							{result.ok ? (
								<CircleCheck className="mt-0.5 size-4 shrink-0" />
							) : (
								<CircleX className="mt-0.5 size-4 shrink-0" />
							)}
							<span className="min-w-0 break-words">{result.detail}</span>
						</div>
					)}
					{!result && requestError && (
						<div
							className="flex items-start gap-2 rounded-md bg-destructive/10 p-3 text-sm text-destructive"
							role="alert"
						>
							<CircleX className="mt-0.5 size-4 shrink-0" />
							<span className="min-w-0 break-words">{requestError}</span>
						</div>
					)}
				</FieldGroup>

				<DialogFooter>
					<Button variant="ghost" onClick={() => onOpenChange(false)} disabled={testing}>
						{t('common.close')}
					</Button>
					<Button onClick={handleTest} disabled={!canSubmit}>
						{testing ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<FlaskConical className="size-3.5" />
						)}
						{testing ? t('credential.test.running') : t('credential.test.run')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}

/** Parse the parameters textarea; `null` when it isn't a JSON object. */
function parseParameters(text: string): Record<string, unknown> | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return null;
	}
	if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
	return parsed as Record<string, unknown>;
}
