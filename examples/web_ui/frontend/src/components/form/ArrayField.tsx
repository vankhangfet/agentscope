import { Plus } from 'lucide-react';
import { useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { ChipList } from '@/components/form/ChipList';
import { Button } from '@/components/ui/button';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';

interface Props {
	/** DOM id for the add-item input; the label points at it. */
	id?: string;
	label: string;
	/** Helper text shown under the control. */
	description?: string;
	/** Placeholder of the add-item input. */
	placeholder?: string;
	/** Hint rendered when the list is empty. */
	emptyHint?: string;
	/** Appends the required-marker to the label, mirroring SchemaForm. */
	required?: boolean;
	value: string[];
	onChange: (value: string[]) => void;
}

/**
 * Editor for `string[]` schema properties — chips with per-item remove,
 * plus an add-item input that commits on Enter, blur, or the trailing
 * button. Blank and duplicate entries are ignored (which also makes a
 * blur-then-click double commit a no-op the second time). `onChange`
 * always emits the full new list, so an emptied list is a deliberate
 * `[]`, not "unset".
 */
export function ArrayField({
	id,
	label,
	description,
	placeholder,
	emptyHint,
	required,
	value,
	onChange,
}: Props) {
	const { t } = useTranslation();
	const [draft, setDraft] = useState('');

	const add = () => {
		const item = draft.trim();
		// Blank and duplicate entries are silently ignored, which also
		// makes a blur-then-click double commit a no-op the second time.
		if (item && !value.includes(item)) onChange([...value, item]);
		setDraft('');
	};

	const remove = (item: string) => onChange(value.filter((v) => v !== item));

	const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
		if (e.key !== 'Enter') return;
		// Commit the row instead of letting Enter submit the enclosing dialog.
		e.preventDefault();
		add();
	};

	return (
		<Field>
			<FieldLabel htmlFor={id}>
				{label}
				{required && <span className="text-destructive ml-0.5">*</span>}
			</FieldLabel>
			{value.length === 0 ? (
				<p className="text-muted-foreground text-sm">
					{emptyHint ?? t('array-field.empty')}
				</p>
			) : (
				<ChipList items={value} onRemove={remove} />
			)}
			<div className="flex gap-2">
				<Input
					id={id}
					value={draft}
					onChange={(e) => setDraft(e.target.value)}
					onKeyDown={handleKeyDown}
					onBlur={add}
					placeholder={placeholder ?? t('array-field.add')}
				/>
				<Button
					type="button"
					variant="outline"
					size="icon"
					onClick={add}
					disabled={!draft.trim()}
					aria-label={t('array-field.add')}
					title={t('array-field.add')}
				>
					<Plus />
				</Button>
			</div>
			{description && <FieldDescription>{description}</FieldDescription>}
		</Field>
	);
}
