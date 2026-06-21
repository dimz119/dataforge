import type { SchemaDiff as SchemaDiffShape } from '../../../shared/api/types';

export interface SchemaDiffProps {
  diff: SchemaDiffShape;
}

/**
 * Additive-only schema diff (frontend-architecture §9.4; INV-REG-3). Under the only
 * MVP compatibility mode (`BACKWARD_ADDITIVE`) a new version can ONLY add optional
 * fields — `removed_fields`/`changed_fields` are empty by construction — so the diff
 * renders the added properties in green and an explicit "removed/changed: none" line
 * that names the contract rather than leaving silence. The shape future-proofs a later
 * non-additive mode (V-2), so we still read the removed/changed arrays defensively.
 */
export function SchemaDiff({ diff }: SchemaDiffProps) {
  const additive = diff.removed_fields.length === 0 && diff.changed_fields.length === 0;
  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface p-4">
      <h3 className="text-sm font-semibold text-text">
        v{diff.from_version} → v{diff.to_version} diff
      </h3>

      {diff.added_fields.length === 0 ? (
        <p className="text-sm text-text-muted">No fields added between these versions.</p>
      ) : (
        <ul className="space-y-1.5">
          {diff.added_fields.map((f) => (
            <li
              key={f.path}
              className="flex flex-wrap items-center gap-2 rounded-md bg-status-green/10 px-3 py-1.5"
            >
              <span aria-hidden="true" className="font-mono text-sm font-semibold text-status-green">
                +
              </span>
              <code className="font-mono text-sm text-status-green">{f.path}</code>
              <span className="rounded bg-status-green/15 px-1.5 py-0.5 text-[11px] font-medium text-status-green">
                {f.type}
              </span>
              <span className="text-[11px] text-text-muted">
                {f.required ? 'required' : 'optional'}
              </span>
            </li>
          ))}
        </ul>
      )}

      <p className="border-t border-border pt-2 text-xs text-text-muted" role="note">
        {additive
          ? 'removed / changed: none — BACKWARD_ADDITIVE (a removal cannot exist, INV-REG-3).'
          : `removed: ${String(diff.removed_fields.length)} · changed: ${String(diff.changed_fields.length)}`}
      </p>
    </div>
  );
}
