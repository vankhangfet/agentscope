import { Ban, Check, Flag, X } from 'lucide-react';
import * as React from 'react';

import type { FactoryPhase, FactoryStatus } from '@/api';
import { Spinner } from '@/components/ui/spinner';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

/** The phases in walk order — the timeline's steps. */
const PHASES: FactoryPhase[] = ['REQUIREMENTS', 'ARCHITECTURE', 'DEVELOP'];

/**
 * The connector between two steps, doubling as the gate that ends the
 * phase on its left: a flag that lights amber while the ticket waits
 * for the human's verdict and turns green once the gate has passed.
 */
function Gate({
	passed,
	waiting,
	final,
}: {
	passed: boolean;
	waiting: boolean;
	final?: boolean;
}) {
	const { t } = useTranslation();
	return (
		<li
			className="flex min-w-8 flex-1 items-center gap-1"
			aria-label={t('factory.timeline.gate')}
			title={t(
				waiting
					? 'factory.timeline.gateWaiting'
					: passed
						? 'factory.timeline.gatePassed'
						: final
							? 'factory.timeline.gateFinal'
							: 'factory.timeline.gate',
			)}
		>
			<span
				className={cn('h-px flex-1', passed ? 'bg-green-500/60' : 'bg-border')}
				aria-hidden="true"
			/>
			<Flag
				aria-hidden="true"
				className={cn(
					'size-3.5 shrink-0',
					waiting
						? 'fill-amber-400/20 text-amber-500'
						: passed
							? 'fill-green-500/20 text-green-600 dark:text-green-400'
							: 'text-muted-foreground/40',
				)}
			/>
			<span
				className={cn('h-px flex-1', passed ? 'bg-green-500/60' : 'bg-border')}
				aria-hidden="true"
			/>
		</li>
	);
}

/** One timeline step: a stateful circle, the phase's name and a
 * one-line caption of what the phase does. */
function Step({
	phase,
	index,
	state,
	status,
}: {
	phase: FactoryPhase;
	index: number;
	state: 'done' | 'current' | 'upcoming';
	status: FactoryStatus;
}) {
	const { t } = useTranslation();
	const failed = state === 'current' && status === 'FAILED';
	const cancelled = state === 'current' && status === 'CANCELLED';
	const waiting = state === 'current' && status === 'WAITING_HUMAN';
	const running = state === 'current' && status === 'RUNNING';
	return (
		<li className="flex shrink-0 items-center gap-2">
			<span
				aria-hidden="true"
				className={cn(
					'flex size-6 items-center justify-center rounded-full border text-[11px] font-semibold',
					state === 'done' &&
						'border-transparent bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300',
					state === 'current' && !failed && !cancelled && 'border-primary text-primary',
					failed &&
						'border-transparent bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300',
					cancelled && 'border-border bg-muted text-muted-foreground',
					state === 'upcoming' && 'border-border text-muted-foreground/60',
				)}
			>
				{state === 'done' ? (
					<Check className="size-3.5" />
				) : failed ? (
					<X className="size-3.5" />
				) : cancelled ? (
					<Ban className="size-3.5" />
				) : running ? (
					<Spinner className="size-3" />
				) : waiting ? (
					<Flag className="size-3.5" />
				) : (
					index + 1
				)}
			</span>
			<span className="flex flex-col items-start leading-tight">
				<span
					className={cn(
						'text-xs',
						state === 'current' ? 'font-semibold text-foreground' : 'text-muted-foreground',
					)}
				>
					{t(`factory.phase.${phase}`)}
				</span>
				{/* One short line under the name: what the phase does. */}
				<span className="text-[10px] text-muted-foreground/80">
					{t(`factory.timeline.caption.${phase}`)}
				</span>
			</span>
		</li>
	);
}

/**
 * The three-phase timeline with its gates: each connector is the gate
 * that ends the phase before it (plus the final gate after DEVELOP),
 * so a WAITING_HUMAN ticket shows exactly where the walk has paused.
 */
export function FactoryPhaseTimeline({
	phase,
	status,
}: {
	phase: FactoryPhase;
	status: FactoryStatus;
}) {
	const { t } = useTranslation();
	const currentIndex = PHASES.indexOf(phase);
	return (
		<ol className="flex w-full items-center gap-2" aria-label={t('factory.timeline.label')}>
			{PHASES.map((p, i) => (
				<React.Fragment key={p}>
					{i > 0 && (
						<Gate
							passed={status === 'COMPLETED' || currentIndex > i - 1}
							waiting={status === 'WAITING_HUMAN' && phase === PHASES[i - 1]}
						/>
					)}
					<Step
						phase={p}
						index={i}
						state={
							status === 'COMPLETED' || i < currentIndex
								? 'done'
								: i === currentIndex
									? 'current'
									: 'upcoming'
						}
						status={status}
					/>
				</React.Fragment>
			))}
			<Gate
				passed={status === 'COMPLETED'}
				waiting={status === 'WAITING_HUMAN' && phase === 'DEVELOP'}
				final
			/>
		</ol>
	);
}
