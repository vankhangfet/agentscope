import { client } from './client';
import type {
	CreateFromPresetRequest,
	CreateFromPresetResponse,
	ListMcpToolsResponse,
	ListPresetsResponse,
	MCPView,
	UpdateMCPRequest,
} from './types';

/**
 * The user's own library of installed MCPs, which is where a hub install
 * lands. Separate from `workspaceApi`, which manages what one session's
 * workspace actually holds.
 */
export const mcpApi = {
	list: () => client.get<MCPView[]>('/mcp'),

	/**
	 * The built-in integration presets — the no-hub path into this
	 * library. Static catalog data with nothing per-user in it, so it
	 * never goes stale while the server is up.
	 */
	presets: () => client.get<ListPresetsResponse>('/mcp/presets'),

	/**
	 * The tools the user's enabled MCPs offer, straight from the library
	 * — unlike the workspace listing this needs no session, so the agent
	 * form can offer names before one exists. Silent by design: a server
	 * that is down just yields an empty group, and a failed listing
	 * degrades the picker to a typed input rather than toasting at the
	 * user mid-dialog.
	 */
	tools: () => client.get<ListMcpToolsResponse>('/mcp/tools', undefined, { silent: true }),

	/**
	 * Fills a preset's fields and adds the result to the library, the
	 * values becoming environment variables on the preset's fixed command.
	 * Like an install this touches no workspace, and the config is not
	 * connection-tested — a wrong token surfaces on first use, not here.
	 * A 409 means the name is taken; a 422 names the missing fields.
	 */
	create: (body: CreateFromPresetRequest, options?: { silent?: boolean }) =>
		client.post<CreateFromPresetResponse>('/mcp', body, undefined, options),

	/**
	 * Renames, enables/disables, or re-keys it. Changing `values` is a
	 * re-render — from the hub card, or from the preset catalog entry when
	 * `hub_id` is `"preset"`; hand-added records have neither, and are
	 * refused.
	 */
	update: (mcpId: string, body: UpdateMCPRequest, options?: { silent?: boolean }) =>
		client.patch<MCPView>(
			`/mcp/${encodeURIComponent(mcpId)}`,
			body,
			undefined,
			options,
		),

	/**
	 * Removes it from the library. Workspaces that already hold this MCP keep
	 * it — the relation is derived by name, not a foreign key.
	 */
	remove: (mcpId: string) => client.delete(`/mcp/${encodeURIComponent(mcpId)}`),
};
