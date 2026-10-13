import { ChevronDown, Plus, Search } from 'lucide-react';
import { useMemo, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { ChipList } from '@/components/form/ChipList';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuCheckboxItem,
	DropdownMenuContent,
	DropdownMenuGroup,
	DropdownMenuLabel,
	DropdownMenuSeparator,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { useMcpTools } from '@/hooks/useMcpTools';

interface Props {
	/** DOM id for the custom-name input; the label points at it. */
	id?: string;
	label: string;
	/** Helper text shown under the control. */
	description?: string;
	/** Placeholder of the custom-name input. */
	placeholder?: string;
	/** Appends the required-marker to the label, mirroring SchemaForm. */
	required?: boolean;
	value: string[];
	onChange: (value: string[]) => void;
}

/**
 * Tools editor for the agent form: the `ArrayField` chips plus a
 * dropdown of the tools the user's enabled MCPs offer, grouped by MCP
 * and filtered by a search over names and descriptions. Picking a tool
 * toggles it; the menu stays open for multi-select. The catalog is
 * advisory, so a custom name can still be typed alongside — and when
 * the listing fails or holds nothing, the dropdown disappears and the
 * field is exactly the typed editor it replaces.
 */
export function ToolsPickerField({
	id,
	label,
	description,
	placeholder,
	required,
	value,
	onChange,
}: Props) {
	const { t } = useTranslation();
	const { groups, hasTools } = useMcpTools();
	const [draft, setDraft] = useState('');
	const [search, setSearch] = useState('');

	const add = () => {
		const item = draft.trim();
		// Blank and duplicate entries are silently ignored, which also
		// makes a blur-then-click double commit a no-op the second time.
		if (item && !value.includes(item)) onChange([...value, item]);
		setDraft('');
	};

	const remove = (item: string) => onChange(value.filter((v) => v !== item));

	const toggle = (tool: string) =>
		onChange(
			value.includes(tool)
				? value.filter((v) => v !== tool)
				: [...value, tool],
		);

	const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
		if (e.key !== 'Enter') return;
		// Commit the row instead of letting Enter submit the enclosing dialog.
		e.preventDefault();
		add();
	};

	const filtered = useMemo(() => {
		const q = search.trim().toLowerCase();
		if (!q) return groups;
		return groups
			.map((group) => ({
				...group,
				tools: group.tools.filter(
					(tool) =>
						tool.name.toLowerCase().includes(q) ||
						tool.description.toLowerCase().includes(q),
				),
			}))
			.filter((group) => group.tools.length > 0);
	}, [groups, search]);

	return (
		<Field>
			<FieldLabel htmlFor={id}>
				{label}
				{required && <span className="text-destructive ml-0.5">*</span>}
			</FieldLabel>
			{value.length === 0 ? (
				<p className="text-muted-foreground text-sm">{t('tools-picker.empty')}</p>
			) : (
				<ChipList items={value} onRemove={remove} />
			)}
			<div className="flex gap-2">
				{hasTools && (
					<DropdownMenu
						onOpenChange={(open) => {
							if (open) setSearch('');
						}}
					>
						<DropdownMenuTrigger asChild>
							<Button
								type="button"
								variant="outline"
								className="justify-between gap-1 font-normal shrink-0"
							>
								<span className="truncate">{t('tools-picker.select')}</span>
								<ChevronDown className="size-3.5 text-muted-foreground" />
							</Button>
						</DropdownMenuTrigger>
						<DropdownMenuContent align="start" className="w-80">
							<div className="flex items-center gap-2 px-2 py-1.5 border-b">
								<Search className="size-3.5 opacity-50" />
								{/* stopPropagation keeps typed letters off Radix's
								    typeahead, which would otherwise move item focus
								    instead of filtering — the TimezoneSelect idiom. */}
								<Input
									value={search}
									onChange={(e) => setSearch(e.target.value)}
									placeholder={t('tools-picker.search')}
									className="h-6 border-0 shadow-none focus-visible:ring-0 text-xs px-0"
									onKeyDown={(e) => e.stopPropagation()}
								/>
							</div>
							<div className="max-h-72 overflow-y-auto">
								{filtered.length === 0 ? (
									<div className="px-2 py-4 text-center text-xs text-muted-foreground">
										{t('tools-picker.no-match')}
									</div>
								) : (
									filtered.map((group) => (
										<DropdownMenuGroup key={group.name}>
											<DropdownMenuLabel>{group.name}</DropdownMenuLabel>
											{group.tools.map((tool) => (
												<DropdownMenuCheckboxItem
													key={tool.name}
													checked={value.includes(tool.name)}
													onCheckedChange={() => toggle(tool.name)}
													// A picker, not a menu: choosing one tool
													// must not close on the first pick.
													onSelect={(e) => e.preventDefault()}
													className="items-start"
												>
													<div className="min-w-0">
														<div className="truncate">{tool.name}</div>
														{tool.description && (
															<div className="text-muted-foreground text-xs truncate">
																{tool.description}
															</div>
														)}
													</div>
												</DropdownMenuCheckboxItem>
											))}
										</DropdownMenuGroup>
									))
								)}
							</div>
							<DropdownMenuSeparator />
							<div className="px-2 py-1.5 text-muted-foreground text-xs">
								{t('tools-picker.custom-hint')}
							</div>
						</DropdownMenuContent>
					</DropdownMenu>
				)}
				<div className="flex flex-1 gap-2">
					<Input
						id={id}
						aria-label={t('tools-picker.custom-label')}
						value={draft}
						onChange={(e) => setDraft(e.target.value)}
						onKeyDown={handleKeyDown}
						onBlur={add}
						placeholder={placeholder}
					/>
					<Button
						type="button"
						variant="outline"
						size="icon"
						onClick={add}
						disabled={!draft.trim()}
						aria-label={t('tools-picker.add')}
						title={t('tools-picker.add')}
					>
						<Plus />
					</Button>
				</div>
			</div>
			{description && <FieldDescription>{description}</FieldDescription>}
		</Field>
	);
}
