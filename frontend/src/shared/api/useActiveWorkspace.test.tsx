import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it } from 'vitest';

import { queryKeys } from './queryKeys';
import type { UserMeResponse } from './types';
import { useActiveWorkspace, useSessionUser } from './useActiveWorkspace';

const USER: UserMeResponse = {
  user_id: 'u-1',
  email: 'ada@example.net',
  is_verified: true,
  created_at: '2026-01-01T00:00:00Z',
  memberships: [
    { workspace_id: 'ws-acme', name: 'Acme', slug: 'acme', role: 'admin' },
    { workspace_id: 'ws-globex', name: 'Globex', slug: 'globex', role: 'member' },
  ],
};

function wrapperFor(slug: string, seed: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (seed) qc.setQueryData(queryKeys.session(), USER);
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[`/w/${slug}`]}>
          <Routes>
            <Route path="/w/:slug" element={<>{children}</>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

describe('useActiveWorkspace', () => {
  it('resolves the slug to the membership UUID + admin flag', () => {
    const { result } = renderHook(() => useActiveWorkspace(), {
      wrapper: wrapperFor('acme', true),
    });
    expect(result.current).toEqual({
      workspaceId: 'ws-acme',
      slug: 'acme',
      name: 'Acme',
      role: 'admin',
      isAdmin: true,
    });
  });

  it('marks non-admin roles as not admin', () => {
    const { result } = renderHook(() => useActiveWorkspace(), {
      wrapper: wrapperFor('globex', true),
    });
    expect(result.current?.isAdmin).toBe(false);
  });

  it('returns null for an unknown slug', () => {
    const { result } = renderHook(() => useActiveWorkspace(), {
      wrapper: wrapperFor('unknown', true),
    });
    expect(result.current).toBeNull();
  });

  it('returns null when the session is not yet cached', () => {
    const { result } = renderHook(() => useActiveWorkspace(), {
      wrapper: wrapperFor('acme', false),
    });
    expect(result.current).toBeNull();
  });

  it('useSessionUser reads the cached session', () => {
    const { result } = renderHook(() => useSessionUser(), {
      wrapper: wrapperFor('acme', true),
    });
    expect(result.current?.email).toBe('ada@example.net');
  });
});
