import { Check, Copy, MousePointerClick, X } from 'lucide-react';
import * as React from 'react';

import { NODE_META, type NodeKind } from './nodeTypes';
import { RunStatusBadge } from './RunStatusBadge';
import type { NodeRunState, WorkflowNode } from '@/api';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { useTranslation } from '@/i18n/useI18n';
import { copyToClipboard } from '@/utils/common';

/**
 * Per-type output keys rendered as plain label/value rows (verbatim —
 * a branch label or verdict is data, not prose); every other value is
 * pretty-printed JSON. `text` always gets its own preformatted block.
 */
const PLAIN_KEYS: Record<NodeKind, string[]> = {
	agent: [],
	condition: ['branch'],
	loop: ['attempts'],
	human_approval: ['verdict', 'note'],
};

/** One label/value row for a plain (primitive) output field. */
function PlainField({ label, value }: { label: string; value: React.ReactNode }) {
	return (
		<div className="flex items-baseline justify-between gap-3">
			<span className="shrink-0 text-xs text-muted-foreground">{label}</span>
			<span className="min-w-0 truncate font-mono text-xs">{value}</span>
		</div>
	);
}

interface NodeOutputPanelProps {
	/** The canvas-selected node; null renders the idle placeholder. */
	node: WorkflowNode | null;
	/** The node's progress (`node_states[id]`), if any yet. */
	state: NodeRunState | null;
	/** The node's blackboard output (`context[id]`, falling back to the
	 * state's copy) — null until the node finishes. */
	output: Record<string, unknown> | null;
	onClose: () => void;
}

/**
 * The run detail's right panel: the selected node's progress (status,
 * attempts, error) and what it wrote to the blackboard — text as a
 * preformatted block, the rest as labelled rows or pretty JSON.
 */
export function NodeOutputPanel({ node, state, output, onClose }: NodeOutputPanelProps) {
	const { t } = useTranslation();
	const [copied, setCopied] = React.useState(false);
	// Drop the copy feedback when the selection (or its output) moves on.
	React.useEffect(() => {
		setCopied(false);
	}, [node?.id, output]);

	if (!node) {
		return (
			<aside className="flex w-80 shrink-0 flex-col border-l">
				<div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-muted-foreground">
					<MousePointerClick className="size-6 opacity-50" />
					<p className="text-center text-xs">
						{t('workflow.runDetail.output.noSelection')}
					</p>
				</div>
			</aside>
		);
	}

	const kind = node.config.type as NodeKind;
	const Icon = NODE_META[kind].icon;
	const plainKeys = new Set(PLAIN_KEYS[kind]);

	const handleCopy = () => {
		void copyToClipboard(JSON.stringify(output ?? null, null, 2)).then((ok) => {
			if (!ok) return;
			setCopied(true);
			window.setTimeout(() => setCopied(false), 1500);
		});
	};

	return (
		<aside className="flex w-80 shrink-0 flex-col border-l">
			<div className="flex items-center gap-2 px-4 pt-4 pb-3">
				<Icon className="size-4 shrink-0" />
				<div className="min-w-0 flex-1 truncate text-sm font-semibold">
					{t(`workflow.builder.palette.${kind}.name`)} · {node.id}
				</div>
				<Button
					variant="ghost"
					size="icon-sm"
					className="text-muted-foreground"
					aria-label={t('common.close')}
					onClick={onClose}
				>
					<X />
				</Button>
			</div>

			<div className="flex-1 space-y-4 overflow-y-auto px-4 pb-6">
				<div className="flex items-center gap-2">
					<RunStatusBadge status={state?.status ?? 'PENDING'} />
					<span className="text-xs text-muted-foreground">
						{t('workflow.runDetail.output.attempts', { count: state?.attempts ?? 0 })}
					</span>
				</div>

				{state?.error && (
					<p className="rounded bg-destructive/10 p-2 font-mono text-[11px] leading-relaxed break-words text-destructive">
						{state.error}
					</p>
				)}

				<Separator />

				<div className="space-y-3">
					<div className="flex items-center justify-between">
						<span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
							{t('workflow.runDetail.output.title')}
						</span>
						{output != null && (
							<Button
								variant="ghost"
								size="icon-sm"
								className="text-muted-foreground"
								aria-label={t('workflow.runDetail.output.copy')}
								onClick={handleCopy}
							>
								{copied ? <Check className="text-green-600" /> : <Copy />}
							</Button>
						)}
					</div>

					{output == null ? (
						<p className="text-xs text-muted-foreground">
							{t('workflow.runDetail.output.noOutput')}
						</p>
					) : (
						Object.entries(output).map(([key, value]) => {
							if (value == null || value === '') return null;
							if (key === 'text') {
								return (
									<pre
										key={key}
										className="max-h-72 overflow-y-auto rounded bg-muted/50 p-2 font-mono text-[11px] leading-relaxed break-words whitespace-pre-wrap"
									>
										{String(value)}
									</pre>
								);
							}
							if (plainKeys.has(key) && ['string', 'number', 'boolean'].includes(typeof value)) {
								return <PlainField key={key} label={key} value={String(value)} />;
							}
							return (
								<div key={key} className="space-y-1">
									<span className="text-xs text-muted-foreground">{key}</span>
									<pre className="max-h-64 overflow-y-auto rounded bg-muted/50 p-2 font-mono text-[11px] leading-relaxed break-all whitespace-pre-wrap">
										{JSON.stringify(value, null, 2)}
									</pre>
								</div>
							);
						})
					)}
				</div>
			</div>
		</aside>
	);
}
