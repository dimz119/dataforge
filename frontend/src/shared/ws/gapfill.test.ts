import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { ApiError } from '../api/problem';
import { gapFill } from './gapfill';

afterEach(() => vi.restoreAllMocks());

describe('gapFill', () => {
  it('pulls pages from the cursor until the live position is reached', async () => {
    const spy = vi.spyOn(api, 'GET').mockImplementation(((_path: string, init: unknown) => {
      const cursor = (init as { params: { query: { cursor: string } } }).params.query.cursor;
      if (cursor === 'c1') {
        return Promise.resolve({
          data: { data: [{ event_type: 'a' }, { event_type: 'b' }], next_cursor: 'c2' },
          error: undefined,
        });
      }
      return Promise.resolve({
        data: { data: [{ event_type: 'c' }], next_cursor: 'c3' },
        error: undefined,
      });
    }) as typeof api.GET);

    const result = await gapFill('s1', 'c1', 'c3');
    expect(result.events).toHaveLength(3);
    expect(result.cursor).toBe('c3');
    expect(result.truncated).toBe(false);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it('stops when an empty poll returns the same cursor (caught up to live)', async () => {
    vi.spyOn(api, 'GET').mockResolvedValue({
      data: { data: [], next_cursor: 'c9' },
      error: undefined,
    } as never);
    const result = await gapFill('s1', 'c9', null);
    expect(result.events).toHaveLength(0);
    expect(result.cursor).toBe('c9');
  });

  it('throws the typed ApiError on cursor-expired (410)', async () => {
    const err = new ApiError({ status: 410, type: 'cursor-expired', title: 'Cursor expired' });
    vi.spyOn(api, 'GET').mockResolvedValue({ data: undefined, error: err } as never);
    await expect(gapFill('s1', 'c1', null)).rejects.toBeInstanceOf(ApiError);
  });
});
