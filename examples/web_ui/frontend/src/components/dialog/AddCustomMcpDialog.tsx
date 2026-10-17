import { CircleAlert, Plus, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import { mcpApi } from '@/api';
import type { MCPClient } from '@/api';
import { ApiError } from '@/api/client';
import { ArrayField } from '@/components/form/ArrayField';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useTranslation } from '@/i18n/useI18n';

/** The backend's name rule — also what workspace references rely on. */
const NAME_PATTERN = /^[a-zA-Z0-9_-]+$/;

/** One editable env/header pair. A blank key marks the row not-yet-typed. */
interface KeyValue {
	key: string;
	value: string;
}

/**
 * Rows of key/value inputs with per-row remove and a trailing add button —
 * the editor both `env` (stdio) and `headers` (http) use. Values are plain
 * text, not password inputs: they are stored server-side and never echoed
 * back, so there is nothing to reveal.
 */
function KeyValueField({
	idPrefix,
	label,
	description,
	keyPlaceholder,
	valuePlaceholder,
	rows,
	onChange,
}: {
	/** DOM id prefix; each row's inputs derive theirs from it. */
	idPrefix: string;
	label: string;
	description?: string;
	keyPlaceholder: string;
	valuePlaceholder: string;
	rows: KeyValue[];
	onChange: (rows: KeyValue[]) => void;
}) {
	const { t } = useTranslation();

	const set = (index: number, patch: Partial<KeyValue>) =>
		onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));

	return (
		<Field>
			<FieldLabel>{label}</FieldLabel>
			{rows.length === 0 ? (
				<p className="text-muted-foreground text-sm">{t('mcp.custom.emptyRows')}</p>
			) : (
				<div className="flex flex-col gap-y-2">
					{rows.map((row, index) => (
						<div key={index} className="flex items-center gap-2">
							<Input
								id={`${idPrefix}-key-${index}`}
								className="font-mono"
								value={row.key}
								onChange={(e) => set(index, { key: e.target.value })}
								placeholder={keyPlaceholder}
								autoComplete="off"
								spellCheck={false}
							/>
							<Input
								id={`${idPrefix}-value-${index}`}
								className="font-mono"
								type="text"
								value={row.value}
								onChange={(e) => set(index, { value: e.target.value })}
								placeholder={valuePlaceholder}
								autoComplete="off"
								spellCheck={false}
							/>
							<Button
								variant="ghost"
								size="icon"
								className="text-muted-foreground shrink-0"
								onClick={() => onChange(rows.filter((_, i) => i !== index))}
								aria-label={t('mcp.custom.removeRow')}
							>
								<Trash2 />
							</Button>
						</div>
					))}
				</div>
			)}
			<Button
				variant="outline"
				size="sm"
				className="self-start"
				onClick={() => onChange([...rows, { key: '', value: '' }])}
			>
				<Plus className="size-3.5" />
				{t('mcp.custom.addKey')}
			</Button>
			{description && <FieldDescription>{description}</FieldDescription>}
		</Field>
	);
}

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	/** Signals the page to refetch the library after a create. */
	onCreated: () => void;
}

/**
 * Adds a hand-written MCP server — the path for servers neither the hubs
 * nor the preset catalog carry: the config is typed in exactly as it
 * should run, stdio command or http endpoint.
 *
 * Mirrors `AddIntegrationDialog` minus the preset: silent request with
 * errors answered inline, a success toast, and a refetch through
 * `onCreated`. No connection test here either — the Test button on the
 * library row is for that.
 */
export function AddCustomMcpDialog({ open, onOpenChange, onCreated }: Props) {
	const { t } = useTranslation();
	const [kind, setKind] = useState<'stdio' | 'http'>('stdio');
	const [name, setName] = useState('');
	const [stateful, setStateful] = useState(true);
	const [command, setCommand] = useState('');
	const [args, setArgs] = useState<string[]>([]);
	const [env, setEnv] = useState<KeyValue[]>([]);
	const [url, setUrl] = useState('');
	const [headers, setHeaders] = useState<KeyValue[]>([]);
	const [saving, setSaving] = useState(false);
	const [error, setError] = useState<string | null>(null);

	// Reset on open, so a previous form's token is never carried over. The
	// content only renders while open, so nothing visibly flashes.
	useEffect(() => {
		if (!open) return;
		setKind('stdio');
		setName('');
		setStateful(true);
		setCommand('');
		setArgs([]);
		setEnv([]);
		setUrl('');
		setHeaders([]);
		setError(null);
	}, [open]);

	const switchKind = (next: 'stdio' | 'http') => {
		setKind(next);
		// The toggle re-tracks the type's default — a stdio process pays
		// for its startup far more than an http connection does.
		setStateful(next === 'stdio');
		setError(null);
	};

	const nameOk = NAME_PATTERN.test(name.trim());
	const valid =
		nameOk && (kind === 'stdio' ? command.trim() !== '' : url.trim() !== '');

	const handleSave = async () => {
		setSaving(true);
		setError(null);
		try {
			// Rows without a key are "not typed yet", dropped rather than
			// sent; a later duplicate key wins over an earlier one, which
			// is how the row order reads.
			const kv = (rows: KeyValue[]) =>
				Object.fromEntries(
					rows
						.filter((row) => row.key.trim() !== '')
						.map((row) => [row.key.trim(), row.value]),
				);
			const envRecord = kv(env);
			const headerRecord = kv(headers);
			const client: MCPClient =
				kind === 'stdio'
					? {
							name: name.trim(),
							is_stateful: stateful,
							mcp_config: {
								type: 'stdio_mcp',
								command: command.trim(),
								...(args.length > 0 ? { args } : {}),
								...(Object.keys(envRecord).length > 0 ? { env: envRecord } : {}),
							},
						}
					: {
							name: name.trim(),
							is_stateful: stateful,
							mcp_config: {
								type: 'http_mcp',
								url: url.trim(),
								...(Object.keys(headerRecord).length > 0
									? { headers: headerRecord }
									: {}),
							},
						};
			// A 409 on the name and a 422 naming the invalid field are
			// both expected and answered inline, so the generic error
			// toast would only duplicate them.
			await mcpApi.createCustom(client, { silent: true });
			toast.success(t('mcp.custom.addedToast', { name: client.name }));
			onCreated();
			onOpenChange(false);
		} catch (e) {
			const detail = e instanceof ApiError ? e.detail : (e as Error).message;
			// The clash detail already names the record; the hint says
			// what to do about it.
			setError(
				e instanceof ApiError && e.status === 409
					? `${detail} ${t('mcp.custom.nameClashHint')}`
					: detail,
			);
		} finally {
			setSaving(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent>
				<DialogHeader>
					<DialogTitle>{t('mcp.custom.title')}</DialogTitle>
					<DialogDescription>{t('mcp.custom.description')}</DialogDescription>
				</DialogHeader>

				<FieldGroup>
					<Field>
						<FieldLabel htmlFor="add-custom-mcp-name">
							{t('mcp.nameLabel')}
							<span className="text-destructive ml-0.5">*</span>
						</FieldLabel>
						<Input
							id="add-custom-mcp-name"
							value={name}
							onChange={(e) => setName(e.target.value)}
							placeholder="my-mcp-server"
							autoComplete="off"
							spellCheck={false}
							className="font-mono"
						/>
						<FieldDescription>{t('mcp.custom.nameHint')}</FieldDescription>
					</Field>

					{/* The type decides which fields render below; it also
					    re-seeds the stateful default (see `switchKind`). */}
					<Tabs value={kind} onValueChange={(v) => switchKind(v as 'stdio' | 'http')}>
						<TabsList className="w-full">
							<TabsTrigger value="stdio">{t('mcp.custom.typeStdio')}</TabsTrigger>
							<TabsTrigger value="http">{t('mcp.custom.typeHttp')}</TabsTrigger>
						</TabsList>
						<TabsContent value="stdio" className="mt-3 flex flex-col gap-4">
							<FieldDescription>{t('mcp.custom.typeStdioDesc')}</FieldDescription>
							<Field>
								<FieldLabel htmlFor="add-custom-mcp-command">
									{t('mcp.custom.command')}
									<span className="text-destructive ml-0.5">*</span>
								</FieldLabel>
								<Input
									id="add-custom-mcp-command"
									value={command}
									onChange={(e) => setCommand(e.target.value)}
									placeholder={t('mcp.custom.commandPlaceholder')}
									autoComplete="off"
									spellCheck={false}
									className="font-mono"
								/>
							</Field>
							<ArrayField
								id="add-custom-mcp-args"
								label={t('mcp.custom.args')}
								placeholder={t('mcp.custom.argsPlaceholder')}
								value={args}
								onChange={setArgs}
							/>
							<KeyValueField
								idPrefix="add-custom-mcp-env"
								label={t('mcp.custom.env')}
								description={t('mcp.custom.envDescription')}
								keyPlaceholder={t('mcp.custom.envKeyPlaceholder')}
								valuePlaceholder={t('mcp.custom.valuePlaceholder')}
								rows={env}
								onChange={setEnv}
							/>
						</TabsContent>
						<TabsContent value="http" className="mt-3 flex flex-col gap-4">
							<FieldDescription>{t('mcp.custom.typeHttpDesc')}</FieldDescription>
							<Field>
								<FieldLabel htmlFor="add-custom-mcp-url">
									{t('mcp.custom.url')}
									<span className="text-destructive ml-0.5">*</span>
								</FieldLabel>
								<Input
									id="add-custom-mcp-url"
									value={url}
									onChange={(e) => setUrl(e.target.value)}
									placeholder={t('mcp.custom.urlPlaceholder')}
									autoComplete="off"
									spellCheck={false}
									className="font-mono"
								/>
							</Field>
							<KeyValueField
								idPrefix="add-custom-mcp-headers"
								label={t('mcp.custom.headers')}
								description={t('mcp.custom.headersDescription')}
								keyPlaceholder={t('mcp.custom.headerKeyPlaceholder')}
								valuePlaceholder={t('mcp.custom.valuePlaceholder')}
								rows={headers}
								onChange={setHeaders}
							/>
						</TabsContent>
					</Tabs>

					<Field>
						<div className="flex flex-row items-center justify-between">
							<div className="flex flex-col gap-y-0.5">
								<FieldLabel>{t('mcp.custom.statefulLabel')}</FieldLabel>
								<span className="text-xs text-muted-foreground">
									{t('mcp.custom.statefulDescription')}
								</span>
							</div>
							<Switch checked={stateful} onCheckedChange={setStateful} />
						</div>
					</Field>
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
					{/* The server re-validates all of this; disabling here is
					    only so an obviously incomplete form is not sent. */}
					<Button onClick={handleSave} disabled={saving || !valid}>
						{saving && <Spinner />}
						{t('common.add')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
