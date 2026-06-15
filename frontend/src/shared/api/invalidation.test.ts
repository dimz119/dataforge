import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import { invalidate } from './invalidation';
import { queryKeys } from './queryKeys';

const WS = 'ws-uuid';
const SID = 'stream-uuid';
const IID = 'instance-uuid';

function makeClient() {
  const qc = new QueryClient();
  const invalidateSpy = vi.spyOn(qc, 'invalidateQueries').mockResolvedValue();
  const removeSpy = vi.spyOn(qc, 'removeQueries').mockReturnValue();
  const clearSpy = vi.spyOn(qc, 'clear').mockReturnValue();
  return { qc, invalidateSpy, removeSpy, clearSpy };
}

describe('invalidation matrix (§4.3)', () => {
  it('logout clears the whole cache', () => {
    const { qc, clearSpy } = makeClient();
    invalidate.onLogout(qc);
    expect(clearSpy).toHaveBeenCalledTimes(1);
  });

  it('session/workspace mutations invalidate their lists', async () => {
    const { qc, invalidateSpy } = makeClient();
    await invalidate.onSession(qc);
    await invalidate.onWorkspaceCreated(qc);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.session() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspaces() });
  });

  it('updateWorkspace invalidates list + detail', async () => {
    const { qc, invalidateSpy } = makeClient();
    await invalidate.onWorkspaceUpdated(qc, WS);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspaces() });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspaceDetail(WS) });
  });

  it('deleteWorkspace invalidates the list and removes the tenant subtree', async () => {
    const { qc, invalidateSpy, removeSpy } = makeClient();
    await invalidate.onWorkspaceDeleted(qc, WS);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspaces() });
    expect(removeSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspace(WS) });
  });

  it('member changes invalidate members, plus workspaces when removing self', async () => {
    const { qc, invalidateSpy } = makeClient();
    await invalidate.onMembersChanged(qc, WS);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.members(WS) });
    invalidateSpy.mockClear();
    await invalidate.onMembersChanged(qc, WS, true);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.workspaces() });
  });

  it('keys / instances / instance-config invalidations target the right key', async () => {
    const { qc, invalidateSpy } = makeClient();
    await invalidate.onKeysChanged(qc, WS);
    await invalidate.onInstancesChanged(qc, WS);
    await invalidate.onInstanceConfigSaved(qc, WS, IID);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.keys(WS) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.instances(WS) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.instance(WS, IID) });
  });

  it('stream lifecycle invalidates detail + list', async () => {
    const { qc, invalidateSpy } = makeClient();
    await invalidate.onStreamCreated(qc, WS);
    await invalidate.onStreamLifecycle(qc, WS, SID);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.streams(WS) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.stream(WS, SID) });
  });

  it('deleteStream invalidates the list and removes the stream subtree', async () => {
    const { qc, invalidateSpy, removeSpy } = makeClient();
    await invalidate.onStreamDeleted(qc, WS, SID);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.streams(WS) });
    expect(removeSpy).toHaveBeenCalledWith({ queryKey: queryKeys.stream(WS, SID) });
  });

  it('chaos update invalidates chaos + stream detail', async () => {
    const { qc, invalidateSpy } = makeClient();
    await invalidate.onChaosUpdated(qc, WS, SID);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.streamChaos(WS, SID) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.stream(WS, SID) });
  });
});
