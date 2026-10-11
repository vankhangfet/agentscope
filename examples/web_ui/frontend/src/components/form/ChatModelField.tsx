import { useTranslation } from 'react-i18next';

import type { ChatModelConfig } from '@/api';
import { LlmSelect } from '@/components/select/LlmSelect';
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field';

interface Props {
	/** DOM id forwarded to the picker trigger; the label points at it. */
	id?: string;
	label: string;
	/** Helper text shown under the control. */
	description?: string;
	/** Trigger placeholder when nothing is selected. */
	placeholder?: string;
	/**
	 * Current selection. `null` is a meaningful state — "no agent-level
	 * override, the session's model config is in charge" — not merely
	 * "nothing picked yet".
	 */
	value: ChatModelConfig | null;
	onChange: (value: ChatModelConfig | null) => void;
}

/**
 * Labelled picker for an agent-level default chat model, composing the
 * house `LlmSelect` (credential → model, emitting a full
 * `ChatModelConfig`). Clearing the selection emits `null`, which the
 * backend persists as "inherit the session-level config".
 */
export function ChatModelField({
	id,
	label,
	description,
	placeholder,
	value,
	onChange,
}: Props) {
	const { t } = useTranslation();
	return (
		<Field>
			<FieldLabel htmlFor={id}>{label}</FieldLabel>
			<LlmSelect
				id={id}
				size="default"
				className="w-full"
				allowClear
				value={value}
				placeholder={placeholder ?? t('chat-model-field.placeholder')}
				onChange={onChange}
			/>
			{description && <FieldDescription>{description}</FieldDescription>}
		</Field>
	);
}
