import type { FactoryPhase } from '@/api';
import { Badge } from '@/components/ui/badge';
import { useTranslation } from '@/i18n/useI18n';

/** A factory phase chip: which of the three phases the ticket is in. */
export function FactoryPhaseBadge({ phase }: { phase: FactoryPhase }) {
	const { t } = useTranslation();
	return <Badge variant="outline">{t(`factory.phase.${phase}`)}</Badge>;
}
