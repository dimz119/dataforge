/**
 * API-key scope catalog (frontend-architecture §9.6 CreateKeyDialog; domain
 * model §5). `answer_key:read` is admin-only — the dialog hides it for members.
 */
import type { ScopesEnum } from '../../shared/api/types';

export interface ScopeSpec {
  value: ScopesEnum;
  label: string;
  description: string;
  /** Only admins may grant this scope (ADR-0017 answer-key gating). */
  adminOnly: boolean;
}

export const SCOPES: readonly ScopeSpec[] = [
  { value: 'events:read', label: 'events:read', description: 'Pull delivered events.', adminOnly: false },
  { value: 'streams:read', label: 'streams:read', description: 'Read stream state and stats.', adminOnly: false },
  { value: 'streams:write', label: 'streams:write', description: 'Create and control streams.', adminOnly: false },
  { value: 'schemas:read', label: 'schemas:read', description: 'Read the schema registry.', adminOnly: false },
  {
    value: 'answer_key:read',
    label: 'answer_key:read',
    description: 'Read ground-truth injections.',
    adminOnly: true,
  },
];

/** The scopes a user with the given admin flag may grant (§9.6). */
export function selectableScopes(isAdmin: boolean): readonly ScopeSpec[] {
  return isAdmin ? SCOPES : SCOPES.filter((s) => !s.adminOnly);
}
