/**
 * Slug derivation for the create-workspace form (frontend-architecture §9.3
 * CreateWorkspaceForm: "slug auto-derived, editable"). Lowercase, hyphen-joined,
 * alphanumeric-only — the authoritative uniqueness check is the API's (`conflict`).
 */
export function deriveSlug(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 50);
}
