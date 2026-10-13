import { useTranslation } from 'react-i18next';

import type {
	AgentSchemaV2Response,
	ChatModelConfig,
	JSONSchema,
	JSONSchemaProperty,
} from '@/api';
import { ChatModelField } from '@/components/form/ChatModelField';
import { SchemaForm, type SchemaFormValue } from '@/components/form/SchemaForm';
import { ToolsPickerField } from '@/components/form/ToolsPickerField';
import { Checkbox } from '@/components/ui/checkbox';
import {
	Field,
	FieldContent,
	FieldDescription,
	FieldGroup,
	FieldLabel,
	FieldLegend,
	FieldSeparator,
	FieldSet,
} from '@/components/ui/field';

export type AgentSection =
	| 'identity'
	| 'template'
	| 'context_config'
	| 'react_config'
	| 'invite_config';

/** Anything a section record may hold — SchemaForm scalars plus the
 * richer values of the hand-placed template widgets. */
export type AgentFormValue = SchemaFormValue | string[] | ChatModelConfig;

export type AgentFormValues = {
	[K in AgentSection]: Record<string, AgentFormValue>;
};

interface Props {
	schema: AgentSchemaV2Response;
	values: AgentFormValues;
	onChange: (section: AgentSection, key: string, value: AgentFormValue) => void;
}

/**
 * Section derivation from the `AgentData` schema. Ordered — controls the
 * visual order of the fieldsets. "identity" carries every top-level
 * property that is not a mode block, so any newly added scalar / boolean
 * / textarea field on `AgentData` shows up in the identity fieldset
 * automatically; the sections below live one level down, inside the
 * `chat_config` mode block.
 *
 * "template" is the exception: its three `chat_config` properties
 * (`chat_model_config` / `tools` / `memory`) are hand-placed dedicated
 * widgets — a nested object and an array, which SchemaForm cannot render
 * meaningfully — so its slice only feeds defaults, never SchemaForm.
 */
const NESTED_SECTIONS: Array<{
	key: Exclude<AgentSection, 'identity' | 'template'>;
	i18n: string;
}> = [
	{ key: 'context_config', i18n: 'context-config' },
	{ key: 'react_config', i18n: 'react-config' },
	{ key: 'invite_config', i18n: 'invite-config' },
];

/** `chat_config` properties rendered by the hand-placed template widgets. */
const TEMPLATE_FIELDS = ['chat_model_config', 'tools', 'memory'] as const;

/** Top-level `AgentData` properties that group one mode's settings. */
const MODE_BLOCKS = ['chat_config'];

const IDENTITY_I18N = 'identity';
const TEMPLATE_I18N = 'template';

const toKebab = (s: string) => s.replace(/_/g, '-');

/** Split the `AgentData` schema into the sections the form renders
 * (currently five: `identity`, `template` + one per `NESTED_SECTIONS`
 * entry). */
function sliceSchema(root: JSONSchema): Record<AgentSection, JSONSchema> {
	const props = root.properties ?? {};
	const chatProps = ((props.chat_config as JSONSchema)?.properties ?? {}) as Record<
		string,
		JSONSchemaProperty
	>;

	const identityProps: Record<string, JSONSchemaProperty> = {};
	for (const [k, prop] of Object.entries(props)) {
		if (MODE_BLOCKS.includes(k)) continue;
		identityProps[k] = prop;
	}

	const identity: JSONSchema = {
		type: 'object',
		title: 'Identity',
		properties: identityProps,
		required: (root.required ?? []).filter((r) => !MODE_BLOCKS.includes(r)),
	};

	const templateProps: Record<string, JSONSchemaProperty> = {};
	for (const k of TEMPLATE_FIELDS) {
		const prop = chatProps[k];
		if (prop) templateProps[k] = prop;
	}
	const template: JSONSchema = {
		type: 'object',
		title: 'Agent Template',
		properties: templateProps,
	};

	return {
		identity,
		template,
		context_config: (chatProps.context_config as JSONSchema) ?? {
			type: 'object',
			properties: {},
		},
		react_config: (chatProps.react_config as JSONSchema) ?? {
			type: 'object',
			properties: {},
		},
		invite_config: (chatProps.invite_config as JSONSchema) ?? {
			type: 'object',
			properties: {},
		},
	};
}

export function AgentFormFields({ schema, values, onChange }: Props) {
	const { t } = useTranslation();
	const sections = sliceSchema(schema.schema);

	const rows: Array<{ key: AgentSection; i18n: string; sectionSchema: JSONSchema }> = [
		{ key: 'identity', i18n: IDENTITY_I18N, sectionSchema: sections.identity },
		{ key: 'template', i18n: TEMPLATE_I18N, sectionSchema: sections.template },
		...NESTED_SECTIONS.map((s) => ({
			key: s.key as AgentSection,
			i18n: s.i18n,
			sectionSchema: sections[s.key],
		})),
	];

	return (
		<FieldGroup>
			{rows.map(({ key: sectionKey, i18n: sectionI18n, sectionSchema }, idx) => {
				const legend = t(`agent-form.${sectionI18n}.legend`, {
					defaultValue: sectionSchema.title ?? sectionKey,
				});
				const description = t(`agent-form.${sectionI18n}.description`, {
					defaultValue: '',
				});
				return (
					<div key={sectionKey}>
						{idx > 0 && <FieldSeparator className="my-0" />}
						<FieldSet>
							<FieldLegend>{legend}</FieldLegend>
							{description && <FieldDescription>{description}</FieldDescription>}
							{sectionKey === 'template' ? (
								<>
									<ChatModelField
										id="agent-form-template-chat-model"
										label={t('agent-form.template.chat-model.label')}
										description={t('agent-form.template.chat-model.description')}
										value={
											(values.template.chat_model_config as
												| ChatModelConfig
												| null
												| undefined) ?? null
										}
										onChange={(v) => onChange('template', 'chat_model_config', v)}
									/>
									<ToolsPickerField
										id="agent-form-template-tools"
										label={t('agent-form.template.tools.label')}
										description={t('agent-form.template.tools.description')}
										placeholder={t('agent-form.template.tools.placeholder')}
										value={(values.template.tools as string[] | undefined) ?? []}
										onChange={(v) => onChange('template', 'tools', v)}
									/>
									{/* Mirrors SchemaForm's boolean layout: a described
									    checkbox stacks label and helper text beside it. */}
									<Field orientation="horizontal">
										<Checkbox
											id="agent-form-template-memory"
											checked={!!values.template.memory}
											onCheckedChange={(checked) =>
												onChange('template', 'memory', !!checked)
											}
										/>
										<FieldContent>
											<FieldLabel
												htmlFor="agent-form-template-memory"
												className="font-normal"
											>
												{t('agent-form.template.memory.label')}
											</FieldLabel>
											<FieldDescription>
												{t('agent-form.template.memory.description')}
											</FieldDescription>
										</FieldContent>
									</Field>
								</>
							) : (
								<SchemaForm
									schema={sectionSchema}
									// Template records can hold richer values, but a
									// SchemaForm-rendered section never does.
									values={values[sectionKey] as Record<string, SchemaFormValue>}
									onChange={(k, v) => onChange(sectionKey, k, v)}
									idPrefix={`agent-form-${sectionI18n}`}
									labelFor={(k, prop) =>
										t(`agent-form.${sectionI18n}.${toKebab(k)}.label`, {
											defaultValue: prop.title ?? k.replace(/_/g, ' '),
										})
									}
									placeholderFor={(k, prop) =>
										t(`agent-form.${sectionI18n}.${toKebab(k)}.placeholder`, {
											defaultValue: prop.description ?? '',
										}) || undefined
									}
									// Only for fields whose label cannot carry the
									// whole story on its own — hence no fallback to
									// `prop.description`, which is English-only and
									// already serves as the placeholder.
									descriptionFor={(k) =>
										t(`agent-form.${sectionI18n}.${toKebab(k)}.description`, {
											defaultValue: '',
										}) || undefined
									}
								/>
							)}
						</FieldSet>
					</div>
				);
			})}
		</FieldGroup>
	);
}

/** Build a fresh `AgentFormValues` populated from each section schema's defaults. */
export function defaultAgentFormValues(schema: AgentSchemaV2Response): AgentFormValues {
	const sections = sliceSchema(schema.schema);
	const fromDefaults = (section: JSONSchema): Record<string, AgentFormValue> => {
		const out: Record<string, AgentFormValue> = {};
		for (const [k, prop] of Object.entries(section.properties ?? {})) {
			if (prop.const !== undefined) continue;
			if (prop.default !== undefined) out[k] = prop.default as AgentFormValue;
		}
		return out;
	};
	return {
		identity: fromDefaults(sections.identity),
		// `tools` carries no `default` in the schema (Pydantic emits
		// `default_factory` results without one), so readers fall back to
		// `[]` at the value sites.
		template: fromDefaults(sections.template),
		context_config: fromDefaults(sections.context_config),
		react_config: fromDefaults(sections.react_config),
		invite_config: fromDefaults(sections.invite_config),
	};
}
