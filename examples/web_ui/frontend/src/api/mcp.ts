import { client } from './client';
import type {
	CreateCustomMcpRequest,
	CreateFromPresetRequest,
	CreateFromPresetResponse,
	ListMcpToolsResponse,
	ListPresetsResponse,
	MCPView,
	TestMcpResult,
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
	 * that is down yields a group with empty tools and an `error` saying
	 * why, so the picker keeps showing it mid-dialog instead of toasting
	 * at the user.
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
	 * Adds a hand-written server config to the library — for servers the
	 * hub and preset catalogs do not carry. Like a preset create this
	 * touches no workspace and is not connection-tested; the record has
	 * no provenance, so re-keying it is refused while rename/enable and
	 * the Test button work as for any MCP. A 409 means the name is
	 * taken; a 422 names the invalid field.
	 */
	createCustom: (
		mcpClient: CreateCustomMcpRequest['client'],
		options?: { silent?: boolean },
	) =>
		client.post<CreateFromPresetResponse>(
			'/mcp/custom',
			{ client: mcpClient } satisfies CreateCustomMcpRequest,
			undefined,
			options,
		),

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

	/**
	 * Checks that one installed MCP actually connects and answers — the
	 * explicit "does my integration work" button, unlike create/update
	 * which never dial the server. A connection failure is reported as
	 * `ok: false` data at HTTP 200 (like the credential test), so the
	 * caller shows it inline; only a missing record is a thrown 404.
	 * Silent so the inline banner, not a toast, carries the outcome.
	 */
	test: (mcpId: string) =>
		client.post<TestMcpResult>(
			`/mcp/${encodeURIComponent(mcpId)}/test`,
			null,
			undefined,
			{ silent: true },
		),
};
