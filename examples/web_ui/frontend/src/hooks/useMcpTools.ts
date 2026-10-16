import { useQuery } from '@tanstack/react-query';

import { mcpApi } from '@/api';
import type { McpToolGroup } from '@/api';

/**
 * The tools available through the user's enabled MCPs, from
 * `GET /mcp/tools`.
 *
 * Answering dials every enabled server, so the result is cached for a
 * minute — enough to keep opening and closing the agent form from
 * re-asking each time, while still picking up a freshly installed MCP
 * on the next open after that.
 */
export function useMcpTools() {
	const { data, isPending, error } = useQuery({
		queryKey: ['mcp-tools'],
		queryFn: () => mcpApi.tools(),
		staleTime: 60_000,
	});

	const groups: McpToolGroup[] = data?.groups ?? [];
	return {
		groups,
		total: data?.total ?? 0,
		loading: isPending,
		error: error as Error | null,
		/**
		 * Whether there is anything worth showing a picker for. The
		 * dropdown opens whenever the listing came back with at least one
		 * enabled MCP — even if every server failed to answer — because a
		 * freshly installed MCP that cannot be reached yet should still be
		 * visible with the reason, not silently hidden, leaving the user
		 * with nothing to pick. Only a request that failed outright (no
		 * groups at all) hides the dropdown and degrades the field to a
		 * typed input.
		 */
		hasTools: !error && groups.length > 0,
	};
}
