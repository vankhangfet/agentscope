import * as React from 'react';


import { NODE_META, type NodeKind } from './nodeTypes';
import { PALETTE_MIME } from './WorkflowCanvas';
import { useTranslation } from '@/i18n/useI18n';

interface NodePaletteProps {
	/** Click-to-add: the page places the node at the canvas center. */
	onAdd: (kind: NodeKind) => void;
}

const KINDS: NodeKind[] = ['agent', 'condition', 'loop', 'human_approval', 'notify'];

/**
 * The left-hand list of addable node types. Each entry is both a
 * button (click adds at the canvas center) and an HTML5 drag source
 * (`application/reactflow` payload) that the canvas' onDrop handler
 * turns into a node under the cursor.
 */
export function NodePalette({ onAdd }: NodePaletteProps) {
	const { t } = useTranslation();

	const onDragStart = (event: React.DragEvent, kind: NodeKind) => {
		event.dataTransfer.setData(PALETTE_MIME, kind);
		event.dataTransfer.effectAllowed = 'move';
	};

	return (
		<aside className="flex w-52 shrink-0 flex-col border-r">
			<div className="px-4 pt-4 pb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
				{t('workflow.builder.palette.title')}
			</div>
			<div className="flex-1 space-y-1.5 overflow-y-auto px-2 pb-3">
				{KINDS.map((kind) => {
					const { icon: Icon, accent } = NODE_META[kind];
					return (
						<button
							key={kind}
							type="button"
							draggable
							onDragStart={(e) => onDragStart(e, kind)}
							onClick={() => onAdd(kind)}
							className="group flex w-full cursor-grab items-start gap-2.5 rounded-lg border bg-card px-2.5 py-2 text-left transition-colors hover:border-ring hover:bg-accent active:cursor-grabbing"
						>
							<span className={`mt-0.5 h-6 w-1 shrink-0 rounded-full ${accent}`} />
							<Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
							<span className="min-w-0">
								<span className="block truncate text-xs font-medium">
									{t(`workflow.builder.palette.${kind}.name`)}
								</span>
								<span className="mt-0.5 block text-[10px] leading-snug text-muted-foreground">
									{t(`workflow.builder.palette.${kind}.description`)}
								</span>
							</span>
						</button>
					);
				})}
			</div>
			<div className="border-t px-4 py-2.5 text-[10px] leading-snug text-muted-foreground">
				{t('workflow.builder.palette.hint')}
			</div>
		</aside>
	);
}
