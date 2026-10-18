import type { FactoryStatus } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Spinner } from '@/components/ui/spinner';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

/**
 * Per-status chip colours, mirroring `DiscussionStatusBadge`'s pairs
 * (tinted background, one-step darker text; dark mode flips to
 * 950/300). WAITING_HUMAN takes the amber pair the approvals views
 * use for "a human is needed".
 */
const STATUS_CLASSES: Record<FactoryStatus, string> = {
	PENDING: 'bg-muted text-muted-foreground',
	RUNNING: 'bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
	WAITING_HUMAN: 'bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300',
	COMPLETED: 'bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300',
	FAILED: 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300',
	CANCELLED: 'bg-muted text-muted-foreground',
};

/** A factory ticket status chip: tinted per status, spinning while RUNNING. */
export function FactoryStatusBadge({
	status,
	className,
}: {
	status: FactoryStatus;
	className?: string;
}) {
	const { t } = useTranslation();
	return (
		<Badge className={cn(STATUS_CLASSES[status], className)}>
			{status === 'RUNNING' && <Spinner data-icon="inline-start" />}
			{t(`factory.status.${status}`)}
		</Badge>
	);
}
