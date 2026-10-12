import { useQuery } from '@tanstack/react-query';

import { mcpApi } from '@/api';

/**
 * The built-in integration presets from `GET /mcp/presets`.
 *
 * Compiled into the server, so the catalog cannot change while it is up —
 * hence `staleTime: Infinity`, which turns every visit to the MCP page
 * after the first into a cache read. (Also why there is no create/update
 * invalidation here: creating from a preset writes to the library, which
 * the page refetches, not to the catalog.)
 */
export function useMcpPresets() {
	const { data, isPending, error, refetch } = useQuery({
		queryKey: ['mcp-presets'],
		queryFn: () => mcpApi.presets(),
		staleTime: Infinity,
	});

	return {
		presets: data?.presets ?? [],
		loading: isPending,
		error: error as Error | null,
		refetch: () => void refetch(),
	};
}
