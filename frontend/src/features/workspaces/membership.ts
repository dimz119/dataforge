/**
 * Sole-admin guard rules (INV-TEN-3, frontend-architecture §9.3). The last admin
 * of a workspace cannot be demoted or removed — the UI disables those actions
 * with an explanatory tooltip, mirroring the API's 409 enforcement. Pure so the
 * MembersTable test can assert the disabled state directly.
 */
import type { Membership } from '../../shared/api/types';

export function adminCount(members: readonly Membership[]): number {
  return members.filter((m) => m.role === 'admin').length;
}

/** True when this member is the only admin and so cannot lose admin/membership. */
export function isSoleAdmin(member: Membership, members: readonly Membership[]): boolean {
  return member.role === 'admin' && adminCount(members) <= 1;
}

export const SOLE_ADMIN_TOOLTIP =
  'A workspace must keep at least one admin (INV-TEN-3). Promote another member first.';
