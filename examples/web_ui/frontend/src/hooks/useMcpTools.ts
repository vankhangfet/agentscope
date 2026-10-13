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
		 * Whether there is anything worth showing a picker for. A failed
		 * query or a library whose servers all came back empty hides the
		 * dropdown, and the field degrades to a typed input — a listing
		 * that cannot be trusted should not be offered.
		 */
		hasTools: !error && groups.some((group) => group.tools.length > 0),
	};
}
