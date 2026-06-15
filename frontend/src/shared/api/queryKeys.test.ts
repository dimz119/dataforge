import { describe, expect, it } from 'vitest';

import { queryKeys, staleTimes } from './queryKeys';

const WS = 'ws-uuid';

describe('queryKeys factory (§4.2)', () => {
  it('keeps session-scoped keys at the root', () => {
    expect(queryKeys.session()).toEqual(['session']);
    expect(queryKeys.workspaces()).toEqual(['workspaces']);
  });

  it('roots every tenant-owned key under ["w", workspaceId] for subtree eviction', () => {
    expect(queryKeys.workspace(WS)).toEqual(['w', WS]);
    expect(queryKeys.workspaceDetail(WS)).toEqual(['w', WS, 'detail']);
    expect(queryKeys.members(WS)).toEqual(['w', WS, 'members']);
    expect(queryKeys.keys(WS)).toEqual(['w', WS, 'keys']);
    // Every tenant key shares the workspace() prefix → one removeQueries evicts all.
    const prefix = queryKeys.workspace(WS);
    for (const key of [
      queryKeys.workspaceDetail(WS),
      queryKeys.members(WS),
      queryKeys.keys(WS),
      queryKeys.scenarios(WS),
      queryKeys.instances(WS),
      queryKeys.streams(WS),
      queryKeys.schemas(WS),
    ]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
  });

  it('normalizes optional filters to null so the tuple stays stable', () => {
    expect(queryKeys.activity(WS)).toEqual(['w', WS, 'activity', null]);
    expect(queryKeys.streams(WS)).toEqual(['w', WS, 'streams', null]);
    expect(queryKeys.streams(WS, { state: 'running' })).toEqual([
      'w',
      WS,
      'streams',
      { state: 'running' },
    ]);
  });

  it('builds nested scenario / stream / schema keys', () => {
    expect(queryKeys.scenario(WS, 'ecommerce')).toEqual(['w', WS, 'scenarios', 'ecommerce']);
    expect(queryKeys.scenarioManifest(WS, 'ecommerce', '1.0.0')).toEqual([
      'w',
      WS,
      'scenarios',
      'ecommerce',
      'manifest',
      '1.0.0',
    ]);
    expect(queryKeys.instance(WS, 'i1')).toEqual(['w', WS, 'instances', 'i1']);
    expect(queryKeys.stream(WS, 's1')).toEqual(['w', WS, 'streams', 's1']);
    expect(queryKeys.streamStats(WS, 's1')).toEqual(['w', WS, 'streams', 's1', 'stats']);
    expect(queryKeys.streamChaos(WS, 's1')).toEqual(['w', WS, 'streams', 's1', 'chaos']);
    expect(queryKeys.streamAnswerKey(WS, 's1', 'duplicates')).toEqual([
      'w',
      WS,
      'streams',
      's1',
      'answer-key',
      'duplicates',
      null,
    ]);
    expect(queryKeys.schemaVersions(WS, 'subj')).toEqual([
      'w',
      WS,
      'schemas',
      'subj',
      'versions',
    ]);
    expect(queryKeys.schemaVersion(WS, 'subj', '2')).toEqual([
      'w',
      WS,
      'schemas',
      'subj',
      'versions',
      '2',
    ]);
  });

  it('marks immutable documents and stats with the right staleTime overrides', () => {
    expect(staleTimes.manifest).toBe(Infinity);
    expect(staleTimes.schemaVersion).toBe(Infinity);
    expect(staleTimes.streamStats).toBe(0);
    expect(staleTimes.session).toBe(5 * 60_000);
  });
});
