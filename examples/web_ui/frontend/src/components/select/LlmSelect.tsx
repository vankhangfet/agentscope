import { ChevronDown, PlusCircle, Ban, SquarePen } from 'lucide-react';
import { useState } from 'react';

import type { ChatModelConfig } from '@/api';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuGroup,
	DropdownMenuItem,
	DropdownMenuLabel,
	DropdownMenuPortal,
	DropdownMenuSeparator,
	DropdownMenuSub,
	DropdownMenuSubContent,
	DropdownMenuSubTrigger,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { useAvailableModels } from '@/hooks/useAvailableModels';
import { useTranslation } from '@/i18n/useI18n.ts';
import { cn } from '@/lib/utils';
import { credentialLabel } from '@/utils/common';

interface Props extends Omit<React.ComponentPropsWithoutRef<typeof Button>, 'onChange' | 'value'> {
	value?: ChatModelConfig | null;
	/**
	 * Called when the user selects a model, or — when `allowClear` is true —
	 * clears the selection (in which case `null` is emitted).
	 */
	onChange?: (value: ChatModelConfig | null) => void;
	onAddCredential?: () => void;
	/** Override the trigger label shown when no model is selected. */
	placeholder?: string;
	/**
	 * When true, append a "clear selection" item to the dropdown that emits
	 * `null` via `onChange`. Used by the fallback selector.
	 */
	allowClear?: boolean;
	/** Override the label of the "clear selection" item. */
	clearLabel?: string;
}

export function LlmSelect({
	value,
	onChange,
	onAddCredential,
	placeholder,
	allowClear = false,
	clearLabel,
	className,
	...props
}: Props) {
	const { groups, loading } = useAvailableModels();
	const { t } = useTranslation();
	// The credential the "custom model name" dialog is targeting, and
	// the free-text name typed into it — null/'' when the dialog is
	// closed. Lets users of OpenAI-compatible endpoints pick a model
	// the provider's built-in card list doesn't name.
	const [customTarget, setCustomTarget] = useState<{
		type: string;
		credentialId: string;
		label: string;
	} | null>(null);
	const [customName, setCustomName] = useState('');
	// Credentials whose model list failed to load come back with an empty
	// `models` array; drop them so they don't render empty submenus
	const groupEntries = Object.entries(groups)
		.map(([type, items]) => [type, items.filter((i) => i.models.length > 0)] as const)
		.filter(([, usable]) => usable.length > 0);
	const hasOptions = groupEntries.length > 0;

	const handleSelect = (type: string, credentialId: string, model: string) => {
		onChange?.({ type, credential_id: credentialId, model, parameters: {} });
	};

	const confirmCustom = () => {
		if (!customTarget || !customName.trim()) return;
		handleSelect(customTarget.type, customTarget.credentialId, customName.trim());
		setCustomTarget(null);
		setCustomName('');
	};

	const customModelItem = (type: string, credentialId: string, label: string) => (
		<DropdownMenuItem
			onSelect={() => {
				setCustomTarget({ type, credentialId, label });
				setCustomName('');
			}}
		>
			<SquarePen className="size-4" />
			<span>{t('llm-select.customModel')}</span>
		</DropdownMenuItem>
	);

	const displayLabel = value?.model
		? value.model
		: loading
			? t('llm-select.loading')
			: (placeholder ?? t('llm-select.placeholder'));

	return (
		<>
			<DropdownMenu>
				<DropdownMenuTrigger asChild>
				<Button
					variant="outline"
					size="sm"
					className={cn('justify-between gap-1 font-normal', className)}
					{...props}
				>
					<span className="truncate">{displayLabel}</span>
					<ChevronDown className="size-3.5 text-muted-foreground" />
				</Button>
			</DropdownMenuTrigger>
			<DropdownMenuContent align="start" className="min-w-48 max-h-72 overflow-y-auto">
				{!loading && !hasOptions ? (
					<div className="px-2 py-3 text-center text-sm text-muted-foreground">
						<p className="font-medium">{t('llm-select.empty.title')}</p>
						<p className="text-xs mt-1">{t('llm-select.empty.description')}</p>
					</div>
				) : (
					groupEntries.map(([type, usable], idx) => {
						const isSingle = usable.length === 1;
						return (
							<DropdownMenuGroup key={type}>
								{idx > 0 && <DropdownMenuSeparator />}
								<DropdownMenuLabel>
									{type.replace(/_credential$/, '')}
								</DropdownMenuLabel>
								{isSingle
									? (
											<>
												{usable[0].models.map((m) => (
													<DropdownMenuItem
														key={m.name}
														onSelect={() =>
															handleSelect(
																type,
																usable[0].credential.id,
																m.name,
															)
														}
													>
														{m.name}
													</DropdownMenuItem>
												))}
												<DropdownMenuSeparator />
												{customModelItem(
													type,
													usable[0].credential.id,
													credentialLabel(usable[0].credential),
												)}
											</>
										)
									: usable.map(({ credential, models }) => (
											<DropdownMenuSub key={credential.id}>
												<DropdownMenuSubTrigger>
													{credentialLabel(credential)}
												</DropdownMenuSubTrigger>
												<DropdownMenuPortal>
													<DropdownMenuSubContent className="max-h-60 overflow-y-auto">
														{models.map((m) => (
															<DropdownMenuItem
																key={m.name}
																onSelect={() =>
																	handleSelect(
																		type,
																		credential.id,
																		m.name,
																	)
																}
															>
																{m.label}
															</DropdownMenuItem>
														))}
														<DropdownMenuSeparator />
														{customModelItem(
															type,
															credential.id,
															credentialLabel(credential),
														)}
													</DropdownMenuSubContent>
												</DropdownMenuPortal>
											</DropdownMenuSub>
										))}
							</DropdownMenuGroup>
						);
					})
				)}
				{(allowClear || onAddCredential) && <DropdownMenuSeparator />}
				{allowClear && (
					<DropdownMenuItem onSelect={() => onChange?.(null)} disabled={!value}>
						<Ban className="size-4" />
						<span>{clearLabel ?? t('llm-select.clear')}</span>
					</DropdownMenuItem>
				)}
				{onAddCredential && (
					<DropdownMenuItem onSelect={onAddCredential}>
						<PlusCircle className="size-4" />
						<span>{t('llm-select.addCredential')}</span>
					</DropdownMenuItem>
				)}
			</DropdownMenuContent>
		</DropdownMenu>
		<Dialog
			open={customTarget !== null}
			onOpenChange={(open) => {
				if (!open) {
					setCustomTarget(null);
					setCustomName('');
				}
			}}
		>
			<DialogContent className="sm:max-w-sm">
				<DialogHeader>
					<DialogTitle>{t('llm-select.customModelTitle')}</DialogTitle>
					<DialogDescription>
						{t('llm-select.customModelDescription', {
							credential: customTarget?.label ?? '',
						})}
					</DialogDescription>
				</DialogHeader>
				<Input
					aria-label={t('llm-select.customModelTitle')}
					placeholder={t('llm-select.customModelPlaceholder')}
					value={customName}
					onChange={(e) => setCustomName(e.target.value)}
					onKeyDown={(e) => {
						if (e.key === 'Enter') {
							e.preventDefault();
							confirmCustom();
						}
					}}
					autoFocus
				/>
				<DialogFooter>
					<Button variant="outline" onClick={() => setCustomTarget(null)}>
						{t('common.cancel')}
					</Button>
					<Button onClick={confirmCustom} disabled={!customName.trim()}>
						{t('common.confirm')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
		</>
	);
}
