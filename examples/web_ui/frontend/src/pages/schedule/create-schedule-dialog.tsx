import { format } from 'date-fns';
import { CheckCircle, ChevronDownIcon, CircleAlert, Loader2, PlusCircle } from 'lucide-react';
import * as React from 'react';

import { parseCronExpression } from './schedule-utils';
import type {
	ChatModelConfig,
	PermissionMode,
	ScheduleRecord,
	UpdateScheduleRequest,
} from '@/api';
import { AgentSelect } from '@/components/select/AgentSelect';
import { LlmSelect } from '@/components/select/LlmSelect';
import { PermissionModeSelect } from '@/components/select/PermissionModeSelect';
import { TimezoneSelect } from '@/components/select/TimezoneSelect';
import { Button } from '@/components/ui/button';
import { Calendar } from '@/components/ui/calendar';
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
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { useAgents } from '@/hooks/useAgents';
import { useSchedules } from '@/hooks/useSchedules';
import { useWorkflows } from '@/hooks/useWorkflows';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	onCreated?: () => void;
	/**
	 * When set, the dialog edits this schedule (PATCH) instead of
	 * creating a new one; the form is prefilled from the record.
	 */
	schedule?: ScheduleRecord | null;
	/** Fires after a successful update. */
	onUpdated?: () => void;
	/**
	 * The update call to patch through — normally the parent list's own
	 * `useSchedules().update`, so the parent refreshes along with the
	 * edit. Falls back to this component's own hook when omitted.
	 */
	onUpdate?: (scheduleId: string, body: UpdateScheduleRequest) => Promise<unknown>;
}

type FreqType = 'once' | 'daily' | 'weekly' | 'monthly';

/** What each fire runs: one agent turn, or one workflow run. */
type TargetType = 'agent' | 'workflow';

interface ScheduleForm {
	name: string;
	description: string;
	freq: FreqType;
	date: Date;
	time: string;
	endDate: Date | undefined;
	agentId: string;
	chatModelConfig: ChatModelConfig | null;
	permissionMode: PermissionMode;
	timezone: string;
	stateful: boolean;
	target: TargetType;
	workflowId: string;
	/** `run_input` as JSON text — validated before submit. */
	runInput: string;
	/**
	 * The edited record's stored cron parses as `custom` — the dialog's
	 * four presets can't represent it, so the cadence controls are
	 * shown disabled and the PATCH omits `cron_expression`, keeping the
	 * stored cadence (the backend leaves absent keys untouched).
	 */
	customCron: boolean;
}

function getDefaultForm(): ScheduleForm {
	const now = new Date();
	const hh = String(now.getHours()).padStart(2, '0');
	const mm = String(now.getMinutes()).padStart(2, '0');
	return {
		name: '',
		description: '',
		freq: 'daily' as FreqType,
		date: now,
		time: `${hh}:${mm}`,
		endDate: undefined as Date | undefined,
		agentId: '',
		chatModelConfig: null as ChatModelConfig | null,
		permissionMode: 'dont_ask' as PermissionMode,
		timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
		stateful: false,
		target: 'agent',
		workflowId: '',
		runInput: '{}',
		customCron: false,
	};
}

/**
 * Rebuild the dialog's date field from a parsed cron expression. The
 * create flow only ever writes these four shapes, so the round trip is
 * lossless for user-made schedules; a custom (e.g. agent-made) cron
 * degrades to the daily preset for display only — the submit keeps the
 * stored cron untouched (see `customCron` on the form).
 */
function dateFromParsedCron(cron: string, startedAt: string, now: Date): Date {
	const parts = cron.trim().split(/\s+/);
	const parsed = parseCronExpression(cron, startedAt);
	switch (parsed.frequency) {
		case 'weekly': {
			// Nearest date carrying the cron's weekday.
			const d = new Date(now);
			const delta = ((parsed.weekday ?? d.getDay()) - d.getDay() + 7) % 7;
			d.setDate(d.getDate() + delta);
			return d;
		}
		case 'monthly':
			return new Date(now.getFullYear(), now.getMonth(), parsed.dayOfMonth ?? now.getDate());
		case 'once': {
			// Cron carries day and month but no year — take the next
			// occurrence of that calendar day.
			const day = Number(parts[2]);
			const month = Number(parts[3]) - 1;
			let d = new Date(now.getFullYear(), month, day);
			if (d < now) d = new Date(now.getFullYear() + 1, month, day);
			return d;
		}
		default:
			return now;
	}
}

/** Prefill the form from an existing schedule record. */
function formFromSchedule(schedule: ScheduleRecord): ScheduleForm {
	const { data } = schedule;
	const now = new Date();
	const parsed = parseCronExpression(data.cron_expression, data.started_at);
	return {
		name: data.name,
		description: data.description,
		// A custom cron (e.g. agent-made) degrades to the daily preset;
		// `customCron` keeps the submit from rewriting the stored one.
		freq: (parsed.frequency === 'custom' ? 'daily' : parsed.frequency) as FreqType,
		date: dateFromParsedCron(data.cron_expression, data.started_at, now),
		time: parsed.time,
		endDate: data.ended_at ? new Date(data.ended_at) : undefined,
		agentId: schedule.agent_id,
		chatModelConfig: data.chat_model_config,
		permissionMode: data.permission_mode,
		timezone: data.timezone,
		stateful: data.stateful,
		target: data.workflow_id ? 'workflow' : 'agent',
		workflowId: data.workflow_id ?? '',
		runInput: JSON.stringify(data.run_input ?? {}, null, 2),
		customCron: parsed.frequency === 'custom',
	};
}

function buildCronExpr(freq: FreqType, time: string, date: Date | undefined): string {
	const [h, m] = time.split(':').map(Number);
	switch (freq) {
		case 'daily':
			return `${m} ${h} * * *`;
		case 'weekly': {
			const weekday = date ? date.getDay() : 1;
			return `${m} ${h} * * ${weekday}`;
		}
		case 'monthly': {
			const monthDay = date ? date.getDate() : 1;
			return `${m} ${h} ${monthDay} * *`;
		}
		default:
			return '';
	}
}

/** Parse JSON object text; `null` when it isn't a valid JSON object. */
function parseJsonObject(text: string): Record<string, unknown> | null {
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return null;
	}
	if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
	return parsed as Record<string, unknown>;
}

function DatePickerButton({
	date,
	onSelect,
	placeholder,
	disabled,
}: {
	date: Date | undefined;
	onSelect: (d: Date | undefined) => void;
	placeholder: string;
	disabled?: boolean;
}) {
	return (
		<Popover>
			<PopoverTrigger asChild>
				<Button
					variant="outline"
					disabled={disabled}
					className="w-32 justify-between font-normal"
				>
					{date ? format(date, 'PPP') : placeholder}
					<ChevronDownIcon className="size-3.5 text-muted-foreground" />
				</Button>
			</PopoverTrigger>
			<PopoverContent className="w-auto p-0" align="start">
				<Calendar
					mode="single"
					selected={date}
					onSelect={onSelect}
					captionLayout="dropdown"
				/>
			</PopoverContent>
		</Popover>
	);
}

export function CreateScheduleDialog({
	open,
	onOpenChange,
	onCreated,
	schedule,
	onUpdated,
	onUpdate,
}: Props) {
	const { t } = useTranslation();
	const { create, update } = useSchedules();
	const { agents } = useAgents();
	const { workflows } = useWorkflows();
	const [form, setForm] = React.useState<ScheduleForm>(getDefaultForm);
	const [loading, setLoading] = React.useState(false);
	const [error, setError] = React.useState('');

	const isEdit = !!schedule;

	React.useEffect(() => {
		if (open) {
			setForm(schedule ? formFromSchedule(schedule) : getDefaultForm());
			setError('');
		}
	}, [open, schedule]);

	React.useEffect(() => {
		// Only the create dialog gets the first-agent default — an edit
		// keeps the record's own agent.
		if (!schedule && agents.length > 0 && !form.agentId) {
			setForm((prev) => ({ ...prev, agentId: agents[0].id }));
		}
	}, [agents, form.agentId, schedule]);

	const set = <K extends keyof ScheduleForm>(key: K, value: ScheduleForm[K]) =>
		setForm((prev) => ({ ...prev, [key]: value }));

	const parsedRunInput = parseJsonObject(form.runInput);
	// The record targets a workflow and the user flipped back to Agent —
	// the API has no way to clear `workflow_id`, so this cannot be saved.
	const switchingBack = isEdit && !!schedule!.data.workflow_id && form.target === 'agent';

	const baseValid = !!form.name.trim() && !!form.date && !!form.time;
	const targetValid =
		form.target === 'workflow' ? !!form.workflowId && parsedRunInput !== null : true;
	const isValid = switchingBack
		? false
		: isEdit
			? baseValid && targetValid
			: // Create in agent mode still requires the agent and model —
				// workflow mode ignores both (placeholders are sent).
				baseValid && targetValid && (form.target === 'workflow' || (!!form.agentId && !!form.chatModelConfig));

	// A workflow-target record whose workflow was deleted in the
	// meantime: keep it visible in the select rather than blanking it.
	const missingWorkflow =
		!!form.workflowId && !workflows.some((w) => w.id === form.workflowId);

	const handleSubmit = async () => {
		setError('');
		if (!isValid) return;
		setLoading(true);
		try {
			let cronExpression: string;

			const d = new Date(form.date!);
			const [h, m] = form.time.split(':').map(Number);
			d.setHours(h, m, 0, 0);

			if (form.freq === 'once') {
				cronExpression = `${m} ${h} ${d.getDate()} ${d.getMonth() + 1} *`;
			} else {
				cronExpression = buildCronExpr(form.freq, form.time, form.date);
			}

			if (schedule) {
				const body = {
					name: form.name.trim(),
					description: form.description.trim(),
					// A custom-origin cron can't be represented by the
					// presets — omit the key so the backend keeps the
					// stored cadence instead of rewriting it (absent keys
					// are left untouched by the update merge).
					...(form.customCron ? {} : { cron_expression: cronExpression }),
					timezone: form.timezone,
					// The agent and model of an existing schedule cannot be
					// patched — the API offers no fields for it.
					...(form.target === 'agent'
						? {
								stateful: form.stateful,
								permission_mode: form.permissionMode,
							}
						: {
								run_input: parsedRunInput!,
								// Send `workflow_id` only on an actual retarget
								// (a different workflow than the record's). A
								// patch that leaves it out skips the backend's
								// existence check, so a schedule whose workflow
								// was deleted stays editable.
								...(schedule.data.workflow_id !== form.workflowId && {
									workflow_id: form.workflowId,
								}),
							}),
				};
				await (onUpdate ?? update)(schedule.id, body);
				onUpdated?.();
			} else if (form.target === 'workflow') {
				await create({
					name: form.name.trim(),
					description: form.description.trim(),
					cron_expression: cronExpression,
					timezone: form.timezone,
					// The API requires `agent_id` / `chat_model_config` even
					// for workflow targets; the backend skips their checks
					// and ignores them at fire time, so empty placeholders
					// are all the schema needs.
					agent_id: '',
					chat_model_config: {
						type: '',
						credential_id: '',
						model: '',
						parameters: {},
					},
					enabled: true,
					workflow_id: form.workflowId,
					run_input: parsedRunInput!,
				});
				onCreated?.();
			} else {
				await create({
					name: form.name.trim(),
					description: form.description.trim(),
					cron_expression: cronExpression,
					timezone: form.timezone,
					agent_id: form.agentId,
					chat_model_config: form.chatModelConfig!,
					enabled: true,
					stateful: form.stateful,
					permission_mode: form.permissionMode,
				});
				onCreated?.();
			}
			onOpenChange(false);
		} catch (e) {
			setError(String(e));
		} finally {
			setLoading(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={onOpenChange}>
			<DialogContent className="!w-[500px] !max-w-[500px]">
				<DialogHeader>
					<DialogTitle>
						{isEdit ? t('schedule.editSchedule.title') : t('schedule.createSchedule.title')}
					</DialogTitle>
					<DialogDescription>
						{isEdit
							? t('schedule.editSchedule.description')
							: t('schedule.createSchedule.description')}
					</DialogDescription>
				</DialogHeader>

				<div className="no-scrollbar -mx-4 max-h-[75vh] overflow-y-auto px-4">
					<FieldGroup className="[&>[data-orientation=horizontal]>:last-child]:w-48">
						<Field>
							<FieldLabel>{t('common.name')}</FieldLabel>
							<Input
								value={form.name}
								onChange={(e) => set('name', e.target.value)}
								placeholder={t('schedule.createSchedule.namePlaceholder')}
							/>
						</Field>

						<Field>
							<FieldLabel>{t('schedule.createSchedule.descriptionLabel')}</FieldLabel>
							<Textarea
								value={form.description}
								onChange={(e) => set('description', e.target.value)}
								placeholder={t('schedule.createSchedule.descriptionPlaceholder')}
								className="min-h-[150px]"
							/>
						</Field>

						{/* Custom-origin cadence (edit only): the presets can't
						    represent it, so the controls sit disabled and the
						    submit keeps the stored cron untouched. */}
						{form.customCron && (
							<FieldDescription>{t('schedule.customCronPreserved')}</FieldDescription>
						)}

						<div className="flex gap-3">
							<Field className="flex-1">
								<FieldLabel>{t('common.date')}</FieldLabel>
								<DatePickerButton
									date={form.date}
									onSelect={(d) => d && set('date', d)}
									placeholder={t('schedule.pickDate')}
									disabled={form.customCron}
								/>
							</Field>
							<Field className="w-48">
								<FieldLabel>{t('common.time')}</FieldLabel>
								<Input
									type="time"
									value={form.time}
									onChange={(e) => set('time', e.target.value)}
									disabled={form.customCron}
									className="appearance-none [&::-webkit-calendar-picker-indicator]:hidden [&::-webkit-calendar-picker-indicator]:appearance-none"
								/>
							</Field>
						</div>

						<Field orientation={'horizontal'}>
							<FieldLabel>{t('schedule.timezone')}</FieldLabel>
							<TimezoneSelect
								size="default"
								value={form.timezone}
								onChange={(v) => set('timezone', v)}
							/>
						</Field>

						<Field orientation={'horizontal'}>
							<FieldLabel>{t('schedule.frequency')}</FieldLabel>
							<Select
								value={form.freq}
								onValueChange={(v) => set('freq', v as FreqType)}
								disabled={form.customCron}
							>
								<SelectTrigger>
									<SelectValue />
								</SelectTrigger>
								<SelectContent>
									<SelectItem value="once">{t('schedule.freqOnce')}</SelectItem>
									<SelectItem value="daily">{t('schedule.freqDaily')}</SelectItem>
									<SelectItem value="weekly">
										{t('schedule.freqWeekly')}
									</SelectItem>
									<SelectItem value="monthly">
										{t('schedule.freqMonthly')}
									</SelectItem>
								</SelectContent>
							</Select>
						</Field>

						<Field orientation={'horizontal'}>
							<FieldLabel
								className={form.freq === 'once' ? 'text-muted-foreground' : ''}
							>
								{t('schedule.endAt')}
							</FieldLabel>
							<DatePickerButton
								date={form.freq === 'once' ? undefined : form.endDate}
								onSelect={(d) => set('endDate', d)}
								placeholder={t('schedule.pickDate')}
								disabled={form.freq === 'once' || form.customCron}
							/>
						</Field>

						{/* ─── Target: one agent turn, or one workflow run ─── */}
						<Field orientation={'horizontal'}>
							<FieldLabel>{t('schedule.target')}</FieldLabel>
							<Select
								value={form.target}
								onValueChange={(v) => set('target', v as TargetType)}
							>
								<SelectTrigger>
									<SelectValue />
								</SelectTrigger>
								<SelectContent>
									<SelectItem value="agent">{t('schedule.targetAgent')}</SelectItem>
									<SelectItem value="workflow">
										{t('schedule.targetWorkflow')}
									</SelectItem>
								</SelectContent>
							</Select>
						</Field>

						{switchingBack && (
							<p className="text-sm text-destructive">
								{t('schedule.targetCannotSwitchBack')}
							</p>
						)}

						{form.target === 'agent' ? (
							<>
								{/* The API cannot patch an existing schedule's
								    agent or model, so they only appear on create. */}
								{!isEdit ? (
									<>
										<Field orientation={'horizontal'}>
											<FieldLabel>{t('common.agent')}</FieldLabel>
											<AgentSelect
												size="default"
												agents={agents}
												value={form.agentId || null}
												onChange={(id) => set('agentId', id)}
												placeholder={t('common.selectAgent')}
											/>
										</Field>

										<Field orientation={'horizontal'}>
											<FieldLabel>{t('common.model')}</FieldLabel>
											<LlmSelect
												size="default"
												value={form.chatModelConfig}
												onChange={(v) => set('chatModelConfig', v)}
											/>
										</Field>
									</>
								) : (
									<FieldDescription>
										{t('schedule.targetAgentFixed')}
									</FieldDescription>
								)}

								<Field orientation={'horizontal'}>
									<FieldLabel>{t('schedule.permissionMode')}</FieldLabel>
									<PermissionModeSelect
										size="default"
										value={form.permissionMode}
										onChange={(v) => set('permissionMode', v)}
									/>
								</Field>

								<Field>
									<div className="flex flex-row items-center justify-between">
										<div className="flex flex-col gap-y-0.5">
											<FieldLabel>{t('schedule.stateful')}</FieldLabel>
											<span className="text-xs text-muted-foreground">
												{t('schedule.statefulDesc')}
											</span>
										</div>
										<Switch
											checked={form.stateful}
											onCheckedChange={(v) => set('stateful', v)}
										/>
									</div>
								</Field>
							</>
						) : (
							<>
								<FieldDescription>
									{t('schedule.targetWorkflowDesc')}
								</FieldDescription>

								<Field orientation={'horizontal'}>
									<FieldLabel>{t('common.workflow')}</FieldLabel>
									{/* No workflows at all and none selected: hint instead
									    of an empty dropdown. An edited record whose
									    workflow was deleted still gets the select, with
									    its current value listed as missing. */}
									{workflows.length > 0 || !!form.workflowId ? (
										<Select
											value={form.workflowId}
											onValueChange={(v) => set('workflowId', v)}
										>
											<SelectTrigger>
												<SelectValue
													placeholder={t('schedule.targetSelectWorkflow')}
												/>
											</SelectTrigger>
											<SelectContent>
												{workflows.map((w) => (
													<SelectItem key={w.id} value={w.id}>
														{w.data.name}
													</SelectItem>
												))}
												{missingWorkflow && (
													// Deleted workflow on an edited record —
													// keep it listed so the current value still
													// shows instead of a blank trigger.
													<SelectItem value={form.workflowId} disabled>
														{t('schedule.targetWorkflowMissing')}
													</SelectItem>
												)}
											</SelectContent>
										</Select>
									) : (
										<span className="text-xs text-muted-foreground">
											{t('schedule.targetNoWorkflows')}
										</span>
									)}
								</Field>

								<Field>
									<FieldLabel>{t('schedule.targetRunInput')}</FieldLabel>
									<Textarea
										value={form.runInput}
										onChange={(e) => set('runInput', e.target.value)}
										className="min-h-[72px] font-mono text-xs"
										spellCheck={false}
									/>
									{parsedRunInput === null ? (
										<FieldError>
											{t('schedule.targetRunInputInvalid')}
										</FieldError>
									) : (
										<FieldDescription>
											{t('schedule.targetRunInputHint')}
										</FieldDescription>
									)}
								</Field>
							</>
						)}

						{error && <p className="text-sm text-destructive">{error}</p>}
					</FieldGroup>
				</div>

				<DialogFooter>
					<Button variant="ghost" onClick={() => onOpenChange(false)} disabled={loading}>
						<CircleAlert className="size-3.5" />
						{t('common.cancel')}
					</Button>
					<Button
						onClick={handleSubmit}
						disabled={loading || !isValid}
						tooltip={
							switchingBack ? t('schedule.targetCannotSwitchBack') : undefined
						}
					>
						{loading ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : isEdit ? (
							<CheckCircle className="size-3.5" />
						) : (
							<PlusCircle className="size-3.5" />
						)}
						{loading
							? isEdit
								? t('common.saving')
								: t('common.creating')
							: isEdit
								? t('common.save')
								: t('common.create')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
