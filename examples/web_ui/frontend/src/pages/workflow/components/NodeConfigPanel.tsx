import { Plus, Trash2, X } from 'lucide-react';
import * as React from 'react';

import { NODE_META, type NodeKind } from './nodeTypes';
import { NODE_ID_PATTERN } from './validateGraph';
import type {
	AgentNodeConfig,
	AgentView,
	ChatModelConfig,
	ConditionNodeConfig,
	HumanApprovalNodeConfig,
	LoopNodeConfig,
	NodeConfig,
	WorkflowNode,
} from '@/api';
import { AgentSelect } from '@/components/select/AgentSelect';
import { LlmSelect } from '@/components/select/LlmSelect';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from '@/components/ui/select';
import { Separator } from '@/components/ui/separator';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from '@/i18n/useI18n';


interface NodeConfigPanelProps {
	node: WorkflowNode;
	agents: AgentView[];
	/** Rename the node; the page rewrites edge endpoints and rejects
	 * invalid or colliding ids (the input is controlled by `node.id`). */
	onRename: (nextId: string) => void;
	onConfigChange: (next: NodeConfig) => void;
	onClose: () => void;
}

/** Small labeled number input that clamps to the config's bounds. */
function NumberField({
	label,
	value,
	min,
	max,
	onChange,
}: {
	label: string;
	value: number | undefined;
	min: number;
	max: number;
	onChange: (value: number) => void;
}) {
	return (
		<div className="space-y-1.5">
			<Label className="text-xs">{label}</Label>
			<Input
				type="number"
				min={min}
				max={max}
				value={value ?? ''}
				onChange={(e) => {
					const parsed = e.target.valueAsNumber;
					if (Number.isFinite(parsed)) {
						onChange(Math.min(max, Math.max(min, Math.round(parsed))));
					}
				}}
			/>
		</div>
	);
}

/** The per-type form editing the selected node's config. */
export function NodeConfigPanel({
	node,
	agents,
	onRename,
	onConfigChange,
	onClose,
}: NodeConfigPanelProps) {
	const { t } = useTranslation();
	const kind = node.config.type as NodeKind;
	const Meta = NODE_META[kind];
	const Icon = Meta.icon;
	const idValid = NODE_ID_PATTERN.test(node.id);

	const patch = (partial: Partial<NodeConfig>) =>
		onConfigChange({ ...node.config, ...partial } as NodeConfig);

	return (
		<aside className="flex w-80 shrink-0 flex-col border-l">
			<div className="flex items-center gap-2 px-4 pt-4 pb-3">
				<Icon className={`size-4 shrink-0`} />
				<div className="min-w-0 flex-1 truncate text-sm font-semibold">
					{t(`workflow.builder.palette.${kind}.name`)}
				</div>
				<Button
					variant="ghost"
					size="icon-sm"
					className="text-muted-foreground"
					aria-label={t('workflow.builder.config.close')}
					onClick={onClose}
				>
					<X />
				</Button>
			</div>

			<div className="flex-1 space-y-4 overflow-y-auto px-4 pb-6">
				<div className="space-y-1.5">
					<Label className="text-xs">{t('workflow.builder.config.idLabel')}</Label>
					<Input
						value={node.id}
						spellCheck={false}
						aria-invalid={!idValid}
						onChange={(e) => onRename(e.target.value)}
					/>
					{!idValid && (
						<p className="text-[11px] text-destructive">
							{t('workflow.builder.config.idInvalid')}
						</p>
					)}
				</div>

				<Separator />

				{node.config.type === 'agent' && (
					<AgentForm
						config={node.config}
						agents={agents}
						onPatch={patch}
					/>
				)}
				{node.config.type === 'condition' && (
					<ConditionForm config={node.config} onPatch={patch} />
				)}
				{node.config.type === 'loop' && (
					<LoopForm config={node.config} agents={agents} onPatch={patch} />
				)}
				{node.config.type === 'human_approval' && (
					<ApprovalForm config={node.config} onPatch={patch} />
				)}
			</div>
		</aside>
	);
}

function AgentForm({
	config,
	agents,
	onPatch,
}: {
	config: AgentNodeConfig;
	agents: AgentView[];
	onPatch: (partial: Partial<AgentNodeConfig>) => void;
}) {
	const { t } = useTranslation();
	return (
		<>
			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.agent.agent')}</Label>
				<AgentSelect
					className="w-full"
					agents={agents}
					value={config.agent_id || null}
					placeholder={t('workflow.builder.config.agent.selectPlaceholder')}
					onChange={(agentId) => onPatch({ agent_id: agentId })}
				/>
			</div>
			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.agent.promptTemplate')}</Label>
				<Textarea
					rows={4}
					className="font-mono text-xs"
					spellCheck={false}
					placeholder="{{input}}"
					value={config.prompt_template ?? ''}
					onChange={(e) => onPatch({ prompt_template: e.target.value })}
				/>
			</div>
			<NumberField
				label={t('workflow.builder.config.agent.maxRetries')}
				value={config.max_retries}
				min={0}
				max={10}
				onChange={(max_retries) => onPatch({ max_retries })}
			/>
		</>
	);
}

function ConditionForm({
	config,
	onPatch,
}: {
	config: ConditionNodeConfig;
	onPatch: (partial: Partial<ConditionNodeConfig>) => void;
}) {
	const { t } = useTranslation();
	const branches = config.branches;

	const updateBranch = (index: number, label: string, rule: string) => {
		const next = branches.map((b, i) => (i === index ? { label, rule } : b));
		onPatch({ branches: next });
	};

	return (
		<>
			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.condition.branches')}</Label>
				<div className="space-y-2">
					{branches.map((branch, i) => (
						<div key={i} className="flex items-center gap-1.5">
							<Input
								className="h-7 w-24 shrink-0 text-xs"
								spellCheck={false}
								aria-label={t('workflow.builder.config.condition.label')}
								placeholder={t('workflow.builder.config.condition.label')}
								value={branch.label}
								onChange={(e) => updateBranch(i, e.target.value, branch.rule)}
							/>
							<Input
								className="h-7 min-w-0 flex-1 text-xs"
								spellCheck={false}
								aria-label={t('workflow.builder.config.condition.rule')}
								placeholder="nodes.check.output.structured.passed == true"
								value={branch.rule}
								onChange={(e) => updateBranch(i, branch.label, e.target.value)}
							/>
							<Button
								variant="ghost"
								size="icon-sm"
								className="shrink-0 text-muted-foreground hover:text-destructive"
								aria-label={t('workflow.builder.config.condition.removeBranch')}
								disabled={branches.length <= 1}
								onClick={() =>
									onPatch({ branches: branches.filter((_, j) => j !== i) })
								}
							>
								<Trash2 />
							</Button>
						</div>
					))}
				</div>
				<Button
					variant="outline"
					size="sm"
					className="w-full"
					onClick={() => onPatch({ branches: [...branches, { label: '', rule: '' }] })}
				>
					<Plus />
					{t('workflow.builder.config.condition.addBranch')}
				</Button>
			</div>
			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.condition.model')}</Label>
				<LlmSelect
					className="w-full"
					allowClear
					value={config.chat_model_config ?? null}
					placeholder={t('workflow.builder.config.condition.modelPlaceholder')}
					onChange={(chat_model_config: ChatModelConfig | null) =>
						onPatch({ chat_model_config })
					}
				/>
				<p className="text-[11px] leading-snug text-muted-foreground">
					{t('workflow.builder.config.condition.modelHint')}
				</p>
			</div>
		</>
	);
}

function LoopForm({
	config,
	agents,
	onPatch,
}: {
	config: LoopNodeConfig;
	agents: AgentView[];
	onPatch: (partial: Partial<LoopNodeConfig>) => void;
}) {
	const { t } = useTranslation();
	return (
		<>
			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.loop.mode')}</Label>
				<Select
					value={config.mode}
					onValueChange={(mode) => onPatch({ mode: mode as LoopNodeConfig['mode'] })}
				>
					<SelectTrigger className="w-full">
						<SelectValue />
					</SelectTrigger>
					<SelectContent>
						<SelectItem value="iterate">
							{t('workflow.builder.config.loop.modeIterate')}
						</SelectItem>
						<SelectItem value="retry_until">
							{t('workflow.builder.config.loop.modeRetryUntil')}
						</SelectItem>
					</SelectContent>
				</Select>
			</div>

			{/* The engine requires a body agent in BOTH modes: retry_until
			    reruns it per attempt, and iterate fails any non-empty
			    list at dispatch with "Node 'x' has no body agent to
			    iterate with" — so the field shows for iterate too, not
			    just retry_until. */}
			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.loop.bodyAgent')}</Label>
				<AgentSelect
					className="w-full"
					agents={agents}
					value={config.body_agent_id || null}
					placeholder={t('workflow.builder.config.agent.selectPlaceholder')}
					onChange={(body_agent_id) => onPatch({ body_agent_id })}
				/>
				{config.mode === 'iterate' && (
					<p className="text-[11px] leading-snug text-muted-foreground">
						{t('workflow.builder.config.loop.bodyAgentIterateHint')}
					</p>
				)}
			</div>

			{config.mode === 'iterate' ? (
				<>
					<div className="space-y-1.5">
						<Label className="text-xs">{t('workflow.builder.config.loop.listTemplate')}</Label>
						<Textarea
							rows={3}
							className="font-mono text-xs"
							spellCheck={false}
							placeholder="{{input.items}}"
							value={config.list_template ?? ''}
							onChange={(e) => onPatch({ list_template: e.target.value })}
						/>
					</div>
					<div className="space-y-1.5">
						<Label className="text-xs">{t('workflow.builder.config.loop.itemTemplate')}</Label>
						<Input
							className="font-mono text-xs"
							spellCheck={false}
							placeholder="{{item}}"
							value={config.item_template ?? ''}
							onChange={(e) => onPatch({ item_template: e.target.value })}
						/>
					</div>
					<label className="flex items-center gap-2 text-sm">
						<Checkbox
							checked={config.parallel ?? false}
							disabled
							onCheckedChange={() => undefined}
						/>
						<span className="text-muted-foreground">
							{t('workflow.builder.config.loop.parallel')}
						</span>
						<Badge variant="outline" className="text-[10px]">
							{t('workflow.builder.config.loop.parallelNote')}
						</Badge>
					</label>
				</>
			) : (
				<>
					<div className="space-y-1.5">
						<Label className="text-xs">{t('workflow.builder.config.loop.verifierPrompt')}</Label>
						<Textarea
							rows={3}
							spellCheck={false}
							placeholder={t('workflow.builder.config.loop.verifierPlaceholder')}
							value={config.verifier_prompt ?? ''}
							onChange={(e) => onPatch({ verifier_prompt: e.target.value })}
						/>
					</div>
					<NumberField
						label={t('workflow.builder.config.loop.maxAttempts')}
						value={config.max_attempts}
						min={1}
						max={20}
						onChange={(max_attempts) => onPatch({ max_attempts })}
					/>
				</>
			)}
		</>
	);
}

function ApprovalForm({
	config,
	onPatch,
}: {
	config: HumanApprovalNodeConfig;
	onPatch: (partial: Partial<HumanApprovalNodeConfig>) => void;
}) {
	const { t } = useTranslation();
	const [draft, setDraft] = React.useState('');
	const approvers = config.approvers ?? [];
	const isCustom = config.timeout_branch !== 'fail';

	const addApprover = () => {
		const value = draft.trim();
		if (!value || approvers.includes(value)) return;
		onPatch({ approvers: [...approvers, value] });
		setDraft('');
	};

	return (
		<>
			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.approval.approvers')}</Label>
				{approvers.length > 0 && (
					<div className="flex flex-wrap gap-1.5">
						{approvers.map((approver) => (
							<Badge key={approver} variant="secondary" className="gap-1 pr-1">
								{approver}
								<button
									type="button"
									className="rounded-full p-0.5 hover:bg-accent"
									aria-label={t('workflow.builder.config.approval.removeApprover')}
									onClick={() =>
										onPatch({ approvers: approvers.filter((a) => a !== approver) })
									}
								>
									<X className="size-3" />
								</button>
							</Badge>
						))}
					</div>
				)}
				<div className="flex gap-1.5">
					<Input
						className="h-7 text-xs"
						placeholder={t('workflow.builder.config.approval.approverPlaceholder')}
						value={draft}
						onChange={(e) => setDraft(e.target.value)}
						onKeyDown={(e) => {
							if (e.key === 'Enter') {
								e.preventDefault();
								addApprover();
							}
						}}
					/>
					<Button variant="outline" size="icon-sm" className="shrink-0" onClick={addApprover}>
						<Plus />
					</Button>
				</div>
				<p className="text-[11px] leading-snug text-muted-foreground">
					{t('workflow.builder.config.approval.approversHint')}
				</p>
			</div>

			<NumberField
				label={t('workflow.builder.config.approval.timeoutHours')}
				value={config.timeout_hours}
				min={1}
				max={720}
				onChange={(timeout_hours) => onPatch({ timeout_hours })}
			/>

			<div className="space-y-1.5">
				<Label className="text-xs">{t('workflow.builder.config.approval.timeoutBranch')}</Label>
				<Select
					value={isCustom ? '__custom__' : 'fail'}
					onValueChange={(value) =>
						onPatch({ timeout_branch: value === 'fail' ? 'fail' : '' })
					}
				>
					<SelectTrigger className="w-full">
						<SelectValue />
					</SelectTrigger>
					<SelectContent>
						<SelectItem value="fail">
							{t('workflow.builder.config.approval.timeoutFail')}
						</SelectItem>
						<SelectItem value="__custom__">
							{t('workflow.builder.config.approval.timeoutCustom')}
						</SelectItem>
					</SelectContent>
				</Select>
				{isCustom && (
					<Input
						spellCheck={false}
						placeholder={t('workflow.builder.config.approval.customBranchPlaceholder')}
						value={config.timeout_branch ?? ''}
						onChange={(e) => onPatch({ timeout_branch: e.target.value })}
					/>
				)}
				<p className="text-[11px] leading-snug text-muted-foreground">
					{t('workflow.builder.config.approval.timeoutBranchHint')}
				</p>
			</div>
		</>
	);
}
