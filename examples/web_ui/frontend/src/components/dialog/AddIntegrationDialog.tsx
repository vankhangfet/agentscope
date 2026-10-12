import { CircleAlert, Eye, EyeOff } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { mcpApi } from '@/api';
import type { IntegrationPreset, MCPView, PresetField } from '@/api';
import { ApiError } from '@/api/client';
import { Alert, AlertDescription } from '@/components/ui/alert.tsx';
import { Button } from '@/components/ui/button.tsx';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog.tsx';
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field.tsx';
import { Input } from '@/components/ui/input.tsx';
import { Spinner } from '@/components/ui/spinner.tsx';
import { useTranslation } from '@/i18n/useI18n';

/** One preset field as a labelled input; secret ones mask, with a reveal toggle. */
function PresetFieldInput({
	field,
	value,
	onChange,
	idPrefix,
}: {
	field: PresetField;
	value: string;
	onChange: (value: string) => void;
	idPrefix: string;
}) {
	const { t } = useTranslation();
	const [visible, setVisible] = useState(false);
	const id = `${idPrefix}-${field.name}`;

	return (
		<Field>
			<FieldLabel htmlFor={id}>{field.label}</FieldLabel>
			<div className="relative">
				<Input
					id={id}
					// Masked unless revealed; the toggle is icon-only, so the
					// aria-label below is all a screen reader gets.
					type={field.secret && !visible ? 'password' : 'text'}
					value={value}
					onChange={(e) => onChange(e.target.value)}
					placeholder={field.placeholder || undefined}
					autoComplete="off"
					className={field.secret ? 'pr-9' : undefined}
				/>
				{field.secret && (
					<button
						type="button"
						tabIndex={-1}
						className="absolute right-1.5 top-1/2 flex size-6 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground hover:text-foreground"
						onClick={() => setVisible((v) => !v)}
						aria-label={t('mcp.integration.toggleVisibility')}
					>
						{visible ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
					</button>
				)}
			</div>
		</Field>
	);
}

interface Props {
	/** The catalog entry to add, or `null` to keep the dialog closed. */
	preset: IntegrationPreset | null;
	/**
	 * Set to re-key an MCP already in the library instead of adding a new
	 * one: the same preset, the same form, a PATCH instead of a POST.
	 */
	editing?: MCPView | null;
	onOpenChange: (open: boolean) => void;
	/** Signals the page to refetch the library after a create. */
	onCreated: () => void;
}

/**
 * Collects a preset's field answers and adds it — or re-keys it.
 *
 * Mirrors `InstallMCPDialog` minus the hub: the values go straight to the
 * server, which turns them into environment variables on the preset's
 * fixed command. On an edit every field starts blank rather than
 * pre-filling secrets the server never sends back — a blank field keeps
 * the stored value, because the server merges.
 */
export function AddIntegrationDialog({ preset, editing, onOpenChange, onCreated }: Props) {
	const { t } = useTranslation();
	const [name, setName] = useState('');
	const [values, setValues] = useState<Record<string, string>>({});
	const [saving, setSaving] = useState(false);
	const [error, setError] = useState<string | null>(null);

	// Reset per preset, so a previous form's token is never carried over.
	useEffect(() => {
		if (!preset) return;
		setName(editing?.name ?? '');
		setValues(Object.fromEntries(preset.fields.map((f) => [f.name, f.default])));
		setError(null);
	}, [preset, editing]);

	const handleSave = async () => {
		if (!preset) return;
		setSaving(true);
		setError(null);
		try {
			if (editing) {
				// Only the keys the user actually filled are sent; the
				// server merges them over what it already holds, so leaving
				// a field blank keeps the existing one.
				const filled = Object.fromEntries(
					Object.entries(values).filter(([, v]) => v !== ''),
				);
				await mcpApi.update(
					editing.id,
					{
						name,
						...(Object.keys(filled).length > 0 ? { values: filled } : {}),
					},
					{ silent: true },
				);
				toast.success(t('mcp.integration.savedToast', { name: preset.name }));
			} else {
				// Empty answers are dropped, not sent as blanks — the
				// backend fills a field's default when none arrives, so a
				// cleared default re-applies instead of failing validation.
				const filled = Object.fromEntries(
					Object.entries(values).filter(([, v]) => v !== ''),
				);
				await mcpApi.create(
					{ preset_id: preset.id, name: name || null, values: filled },
					// A 409 on the name and a 422 naming missing fields are
					// both expected and answered inline, so the generic
					// error toast would only duplicate them.
					{ silent: true },
				);
				toast.success(t('mcp.integration.addedToast', { name: preset.name }));
			}
			onCreated();
			onOpenChange(false);
		} catch (e) {
			setError(e instanceof ApiError ? e.detail : (e as Error).message);
		} finally {
			setSaving(false);
		}
	};

	return (
		<Dialog open={preset !== null} onOpenChange={onOpenChange}>
			<DialogContent>
				<DialogHeader>
					<DialogTitle>
						{t(editing ? 'mcp.integration.configureTitle' : 'mcp.integration.addTitle', {
							name: preset?.name ?? '',
						})}
					</DialogTitle>
					<DialogDescription>
						{t(
							editing
								? 'mcp.integration.configureDescription'
								: 'mcp.integration.addDescription',
						)}
					</DialogDescription>
				</DialogHeader>

				<FieldGroup>
					<Field>
						<FieldLabel htmlFor="add-integration-name">{t('mcp.nameLabel')}</FieldLabel>
						<Input
							id="add-integration-name"
							value={name}
							onChange={(e) => setName(e.target.value)}
							placeholder={preset?.id}
						/>
						{!editing && (
							<FieldDescription>
								{t('mcp.integration.nameHint', { id: preset?.id ?? '' })}
							</FieldDescription>
						)}
					</Field>

					{preset?.fields.map((field) => (
						<PresetFieldInput
							key={field.name}
							field={field}
							value={values[field.name] ?? ''}
							onChange={(v) => setValues((prev) => ({ ...prev, [field.name]: v }))}
							idPrefix={`preset-${preset.id}`}
						/>
					))}
				</FieldGroup>

				{error && (
					<Alert variant="destructive">
						<CircleAlert />
						<AlertDescription>{error}</AlertDescription>
					</Alert>
				)}

				<DialogFooter>
					<Button variant="outline" onClick={() => onOpenChange(false)}>
						{t('common.cancel')}
					</Button>
					{/* The name is only required on an edit — a create falls
					    back to the preset id, shown as the placeholder. */}
					<Button onClick={handleSave} disabled={saving || (!!editing && !name)}>
						{saving && <Spinner />}
						{t(editing ? 'common.save' : 'common.add')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
