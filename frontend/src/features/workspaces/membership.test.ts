import { describe, expect, it } from 'vitest';

import type { Membership } from '../../shared/api/types';
import { adminCount, isSoleAdmin } from './membership';

const m = (user_id: string, role: string): Membership => ({
  user_id,
  email: `${user_id}@example.net`,
  role,
  joined_at: '2026-01-01T00:00:00Z',
});

describe('membership (INV-TEN-3)', () => {
  it('counts admins', () => {
    expect(adminCount([m('a', 'admin'), m('b', 'member'), m('c', 'admin')])).toBe(2);
  });

  it('flags the sole admin as locked', () => {
    const members = [m('a', 'admin'), m('b', 'member')];
    expect(isSoleAdmin(members[0], members)).toBe(true);
    expect(isSoleAdmin(members[1], members)).toBe(false);
  });

  it('unlocks once a second admin exists', () => {
    const members = [m('a', 'admin'), m('b', 'admin')];
    expect(isSoleAdmin(members[0], members)).toBe(false);
  });
});
