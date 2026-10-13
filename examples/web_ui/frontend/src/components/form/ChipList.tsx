import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';

interface Props {
	/** The chips to show, in list order. */
	items: string[];
	/** Drop one entry; what an emptied list means is the caller's call. */
	onRemove: (item: string) => void;
}

/**
 * The chips row shared by the string-array editors — one removable badge
 * per entry. Presentation only: dedupe rules and the empty-list hint
 * stay with the editors, which differ on both.
 */
export function ChipList({ items, onRemove }: Props) {
	const { t } = useTranslation();
	return (
		<ul className="flex flex-wrap gap-1.5">
			{items.map((item) => (
				<li key={item}>
					<Badge variant="secondary" className="pl-2">
						{item}
						<button
							type="button"
							className="text-muted-foreground hover:text-foreground -mr-1 flex size-4 items-center justify-center rounded-full hover:bg-foreground/10"
							onClick={() => onRemove(item)}
							aria-label={t('array-field.remove', { item })}
						>
							<X className="size-3" />
						</button>
					</Badge>
				</li>
			))}
		</ul>
	);
}
