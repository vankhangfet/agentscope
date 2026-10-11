import { useTranslation } from '@/i18n/useI18n';

/**
 * Placeholder for the approvals inbox (Task 6). Only the route's page
 * shell lives here so `/workflow/approvals` compiles; the inbox of
 * WAITING_HUMAN runs replaces this file wholesale.
 */
export default function WorkflowApprovalsPage() {
	const { t } = useTranslation();

	return (
		<div className="flex size-full p-2">
			<main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[22px] bg-card shadow-panel">
				<div className="px-6 pt-5 pb-4">
					<div className="text-2xl font-semibold">{t('workflow.approvals.title')}</div>
					<div className="mt-1 text-sm text-muted-foreground">
						{t('workflow.approvals.comingSoon')}
					</div>
				</div>
			</main>
		</div>
	);
}
