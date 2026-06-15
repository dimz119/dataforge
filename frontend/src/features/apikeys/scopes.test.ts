import { describe, expect, it } from 'vitest';

import { SCOPES, selectableScopes } from './scopes';

describe('scopes (§9.6)', () => {
  it('exposes answer_key:read only to admins', () => {
    const adminScopes = selectableScopes(true).map((s) => s.value);
    const memberScopes = selectableScopes(false).map((s) => s.value);
    expect(adminScopes).toContain('answer_key:read');
    expect(memberScopes).not.toContain('answer_key:read');
  });

  it('keeps non-admin scopes available to members', () => {
    const memberScopes = selectableScopes(false).map((s) => s.value);
    expect(memberScopes).toEqual(['events:read', 'streams:read', 'streams:write', 'schemas:read']);
  });

  it('marks exactly one scope admin-only', () => {
    expect(SCOPES.filter((s) => s.adminOnly)).toHaveLength(1);
  });
});
