import type { FactoryMode } from '@/api';
import { Badge } from '@/components/ui/badge';
import { useTranslation } from '@/i18n/useI18n';

/**
 * A gate-mode chip: who answers the ticket's gates — a human every
 * time (COLLABORATIVE) or auto-approve except the final gate
 * (DELEGATED).
 */
export function FactoryModeBadge({ mode }: { mode: FactoryMode }) {
	const { t } = useTranslation();
	return (
		<Badge variant="secondary" className="font-normal">
			{t(`factory.mode.${mode}`)}
		</Badge>
	);
}
