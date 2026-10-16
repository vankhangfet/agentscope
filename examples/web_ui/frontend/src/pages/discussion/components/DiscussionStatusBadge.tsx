import type { DiscussionStatus } from '@/api';
import { Badge } from '@/components/ui/badge';
import { Spinner } from '@/components/ui/spinner';
import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

/**
 * Per-status chip colours, mirroring `RunStatusBadge`'s pairs (tinted
 * background, one-step darker text; dark mode flips to 950/300).
 */
const STATUS_CLASSES: Record<DiscussionStatus, string> = {
	PENDING: 'bg-muted text-muted-foreground',
	RUNNING: 'bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
	COMPLETED: 'bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300',
	FAILED: 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300',
	CANCELLED: 'bg-muted text-muted-foreground',
};

/** A discussion status chip: tinted per status, spinning while RUNNING. */
export function DiscussionStatusBadge({
	status,
	className,
}: {
	status: DiscussionStatus;
	className?: string;
}) {
	const { t } = useTranslation();
	return (
		<Badge className={cn(STATUS_CLASSES[status], className)}>
			{status === 'RUNNING' && <Spinner data-icon="inline-start" />}
			{t(`discussion.status.${status}`)}
		</Badge>
	);
}
