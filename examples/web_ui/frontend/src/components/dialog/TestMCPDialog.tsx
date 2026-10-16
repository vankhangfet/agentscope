import { CircleCheck, CircleX, FlaskConical, Loader2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import { mcpApi } from '@/api';
import type { MCPView, TestMcpResult } from '@/api';
import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from '@/components/ui/dialog';
import { Spinner } from '@/components/ui/spinner';
import { useTranslation } from '@/i18n/useI18n';

interface Props {
	/** The installed MCP to test, or `null` to keep the dialog shut. */
	mcp: MCPView | null;
	onOpenChange: (open: boolean) => void;
}

/**
 * "Test connection" dialog for one installed MCP. Runs
 * `POST /mcp/{id}/test` as soon as it opens — connecting is the whole
 * point, so there is nothing to configure first — and shows the result
 * inline: a green banner with the tool count on success, a red one with
 * the reason on failure. The endpoint reports a failed connection as
 * `ok: false` data, so only a transport-level error (server down, the
 * record gone) lands in the catch and the red banner too.
 *
 * The run is re-triggered on each open and by the "Test again" button,
 * which is useful right after fixing a key in the edit form.
 */
export function TestMCPDialog({ mcp, onOpenChange }: Props) {
	const { t } = useTranslation();
	const [testing, setTesting] = useState(false);
	const [result, setResult] = useState<TestMcpResult | null>(null);
	const [requestError, setRequestError] = useState('');

	const run = useCallback(async (mcpId: string) => {
		setTesting(true);
		setResult(null);
		setRequestError('');
		try {
			setResult(await mcpApi.test(mcpId));
		} catch (e) {
			// Transport-level failure only — the endpoint reports a bad
			// connection as ok=false data, which lands in `result`.
			setRequestError(e instanceof Error ? e.message : String(e));
		} finally {
			setTesting(false);
		}
	}, []);

	// Fire the test the moment the dialog opens on a given MCP; a new
	// `mcp.id` (reopening on a different row) re-runs it.
	useEffect(() => {
		if (!mcp) return;
		void run(mcp.id);
	}, [mcp, run]);

	const name = mcp?.display_name || mcp?.name || '';

	return (
		<Dialog open={mcp !== null} onOpenChange={onOpenChange}>
			<DialogContent className="!w-[480px] !max-w-[480px]">
				<DialogHeader>
					<DialogTitle>{t('mcp.test.title', { name })}</DialogTitle>
					<DialogDescription>{t('mcp.test.description')}</DialogDescription>
				</DialogHeader>

				<div className="min-h-[64px]">
					{testing ? (
						<div className="flex items-center gap-2 rounded-md bg-muted/50 p-3 text-sm text-muted-foreground">
							<Spinner className="size-4" />
							<span>{t('mcp.test.running')}</span>
						</div>
					) : result ? (
						<div
							className={
								result.ok
									? 'flex items-start gap-2 rounded-md bg-emerald-500/10 p-3 text-sm text-emerald-700 dark:text-emerald-400'
									: 'flex items-start gap-2 rounded-md bg-destructive/10 p-3 text-sm text-destructive'
							}
							role="status"
						>
							{result.ok ? (
								<CircleCheck className="mt-0.5 size-4 shrink-0" />
							) : (
								<CircleX className="mt-0.5 size-4 shrink-0" />
							)}
							<div className="min-w-0">
								<div className="font-medium">
									{t(
										result.ok
											? 'mcp.test.successTitle'
											: 'mcp.test.failureTitle',
									)}
								</div>
								<span className="min-w-0 break-words">{result.detail}</span>
							</div>
						</div>
					) : requestError ? (
						<div
							className="flex items-start gap-2 rounded-md bg-destructive/10 p-3 text-sm text-destructive"
							role="alert"
						>
							<CircleX className="mt-0.5 size-4 shrink-0" />
							<div className="min-w-0">
								<div className="font-medium">{t('mcp.test.failureTitle')}</div>
								<span className="min-w-0 break-words">{requestError}</span>
							</div>
						</div>
					) : null}
				</div>

				<DialogFooter>
					<Button
						variant="ghost"
						onClick={() => onOpenChange(false)}
						disabled={testing}
					>
						{t('mcp.test.close')}
					</Button>
					<Button
						onClick={() => mcp && run(mcp.id)}
						disabled={testing || !mcp}
					>
						{testing ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<FlaskConical className="size-3.5" />
						)}
						{testing ? t('mcp.test.running') : t('mcp.test.run')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
