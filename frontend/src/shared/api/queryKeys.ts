/**
 * The single query-key factory (frontend-architecture §4.2). String-literal keys
 * outside this module fail lint — every feature derives keys from here.
 *
 * Two rules give the whole scheme:
 *  1. Session-scoped keys live at the root: `['session']`, `['workspaces']`.
 *  2. Everything tenant-owned lives under `['w', workspaceId, …]` — the workspace
 *     UUID (not the slug; INV-TEN-1). Removing the `['w', wsId]` prefix evicts a
 *     whole tenant subtree in one call.
 *
 * Cursor-paginated endpoints (ADR-0014) always use `useInfiniteQuery` with
 * `getNextPageParam: (last) => last.next_cursor ?? undefined` — the cursor is
 * opaque and never parsed client-side (§4.2).
 */

/** `as const` so each key is a readonly tuple TanStack Query can structurally match. */
export const queryKeys = {
  // --- Session-scoped (root) ---
  session: () => ['session'] as const,
  workspaces: () => ['workspaces'] as const,

  // --- Tenant subtree root (prefix removal evicts everything for a workspace) ---
  workspace: (wsId: string) => ['w', wsId] as const,
  workspaceDetail: (wsId: string) => ['w', wsId, 'detail'] as const,
  quotas: (wsId: string) => ['w', wsId, 'quotas'] as const,
  members: (wsId: string) => ['w', wsId, 'members'] as const,
  activity: (wsId: string, filters?: unknown) => ['w', wsId, 'activity', filters ?? null] as const,
  keys: (wsId: string) => ['w', wsId, 'keys'] as const,

  // --- Scenarios & instances ---
  scenarios: (wsId: string) => ['w', wsId, 'scenarios'] as const,
  scenario: (wsId: string, scenarioSlug: string) =>
    ['w', wsId, 'scenarios', scenarioSlug] as const,
  scenarioManifest: (wsId: string, scenarioSlug: string, version: string) =>
    ['w', wsId, 'scenarios', scenarioSlug, 'manifest', version] as const,
  instances: (wsId: string) => ['w', wsId, 'instances'] as const,
  instance: (wsId: string, instanceId: string) => ['w', wsId, 'instances', instanceId] as const,

  // --- Streams ---
  streams: (wsId: string, filters?: unknown) => ['w', wsId, 'streams', filters ?? null] as const,
  stream: (wsId: string, streamId: string) => ['w', wsId, 'streams', streamId] as const,
  streamStats: (wsId: string, streamId: string) =>
    ['w', wsId, 'streams', streamId, 'stats'] as const,
  streamChaos: (wsId: string, streamId: string) =>
    ['w', wsId, 'streams', streamId, 'chaos'] as const,
  streamAnswerKey: (wsId: string, streamId: string, mode: string, filters?: unknown) =>
    ['w', wsId, 'streams', streamId, 'answer-key', mode, filters ?? null] as const,
  streamSchemaVersions: (wsId: string, streamId: string) =>
    ['w', wsId, 'streams', streamId, 'schema-versions'] as const,
  streamSchemaUpgrades: (wsId: string, streamId: string) =>
    ['w', wsId, 'streams', streamId, 'schema-upgrades'] as const,

  // --- Schema registry (Phase 10 surfaces; keys reserved per §4.2) ---
  schemas: (wsId: string) => ['w', wsId, 'schemas'] as const,
  schema: (wsId: string, subject: string) => ['w', wsId, 'schemas', subject] as const,
  schemaVersions: (wsId: string, subject: string) =>
    ['w', wsId, 'schemas', subject, 'versions'] as const,
  schemaVersion: (wsId: string, subject: string, version: string) =>
    ['w', wsId, 'schemas', subject, 'versions', version] as const,
  schemaDiff: (wsId: string, subject: string, from: number, to: number) =>
    ['w', wsId, 'schemas', subject, 'diff', from, to] as const,
} as const;

/**
 * Per-query staleTime overrides (§4.2 table). Queries not listed here inherit the
 * §4.1 default (30 s). `Infinity` marks immutable documents (manifests INV-CAT-1,
 * schema versions INV-REG-2). Stats use 0 (always stale → polls every 5 s, §4.4).
 */
export const staleTimes = {
  session: 5 * 60_000,
  workspaces: 60_000,
  scenarios: 5 * 60_000,
  scenario: 5 * 60_000,
  manifest: Infinity,
  schemas: 5 * 60_000,
  schemaVersions: 5 * 60_000,
  schemaVersion: Infinity,
  streamStats: 0,
} as const;
