import { Users } from 'lucide-react';

import type { DiscussionData } from '@/api';
import { AgentAvatar } from '@/components/agent/AgentAvatar';
import { useTranslation } from '@/i18n/useI18n';

/** The model label for a chat-model config: its `model`, or a dash. */
function modelLabel(config: { model?: string } | null | undefined): string {
	return config?.model || '—';
}

/**
 * The discussion's setup, shown on the detail page so a user can see
 * who is arguing and how it is judged — not just the arguments the
 * debate produced. Each debater lists its name, model and stance
 * (system prompt); the judge lists its model and, when one was given,
 * its prompt; the round ceiling closes it out. Pure display — editing
 * happens through the "Edit & rerun" action, which seeds the create
 * dialog from this same data.
 */
export function DiscussionSetupPanel({ discussion }: { discussion: DiscussionData }) {
	const { t } = useTranslation();
	const judgePrompt = discussion.judge.system_prompt?.trim();

	return (
		<section className="space-y-3 rounded-xl border border-border bg-muted/20 p-4">
			<div className="flex items-center gap-2 text-sm font-semibold">
				<Users className="size-4 text-muted-foreground" />
				{t('discussion.setup.title')}
				<span className="rounded bg-muted px-2 py-0.5 font-mono text-[10px] font-normal text-muted-foreground">
					{t('discussion.setup.debaterCount', { count: discussion.debaters.length })}
				</span>
			</div>

			{/* Debaters */}
			<div className="grid gap-2 sm:grid-cols-2">
				{discussion.debaters.map((debater) => (
					<div
						key={debater.name}
						className="flex flex-col gap-1.5 rounded-lg border border-border bg-background/60 p-3"
					>
						<div className="flex items-center gap-2">
							<AgentAvatar name={debater.name} size="sm" title={debater.name} />
							<span className="min-w-0 flex-1 truncate text-xs font-semibold">
								{debater.name}
							</span>
							<span
								className="shrink-0 font-mono text-[10px] text-muted-foreground"
								title={modelLabel(debater.chat_model_config)}
							>
								{modelLabel(debater.chat_model_config)}
							</span>
						</div>
						{debater.system_prompt.trim() !== '' ? (
							<p className="line-clamp-3 text-xs leading-relaxed break-words text-muted-foreground">
								{debater.system_prompt}
							</p>
						) : (
							<p className="text-xs italic text-muted-foreground/60">
								{t('discussion.setup.noStance')}
							</p>
						)}
					</div>
				))}
			</div>

			{/* Judge */}
			<div className="flex flex-col gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
				<div className="flex items-center gap-2">
					<AgentAvatar name="judge" size="sm" title={t('discussion.setup.judge')} />
					<span className="min-w-0 flex-1 truncate text-xs font-semibold">
						{t('discussion.setup.judge')}
					</span>
					<span
						className="shrink-0 font-mono text-[10px] text-muted-foreground"
						title={modelLabel(discussion.judge.chat_model_config)}
					>
						{modelLabel(discussion.judge.chat_model_config)}
					</span>
				</div>
				{judgePrompt ? (
					<p className="line-clamp-3 text-xs leading-relaxed break-words text-muted-foreground">
						{judgePrompt}
					</p>
				) : (
					<p className="text-xs italic text-muted-foreground/60">
						{t('discussion.setup.defaultJudge')}
					</p>
				)}
			</div>

			<div className="text-xs text-muted-foreground">
				{t('discussion.setup.maxRounds', { max: discussion.max_rounds })}
			</div>
		</section>
	);
}
