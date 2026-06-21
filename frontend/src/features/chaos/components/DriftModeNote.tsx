import type { DriftSubjectMenu } from '../api';

export interface DriftModeNoteProps {
  /** The per-business-subject drift menu (DR-1). */
  menu: DriftSubjectMenu[];
  /** True when at least one subject has a registerable next version (CH-V07). */
  eligible: boolean;
}

/**
 * DriftModeNote (frontend-architecture §9.5; chaos-engine §5.5). Explains the
 * `schema_drift` arming state below the chaos grid:
 *  - INELIGIBLE (CH-V07): no business subject has a registered version above its
 *    effective version, so drift has nothing to inject — names the highest registered
 *    version per subject so the reader knows what to register/upgrade past.
 *  - ARMED: lists the injectable next-version fields per subject ("v2 adds shipping_state"),
 *    type-synthesized from the chaos sub-seed (never bound, never into cdc.before).
 */
export function DriftModeNote({ menu, eligible }: DriftModeNoteProps) {
  if (menu.length === 0) return null;

  if (!eligible) {
    return (
      <div
        role="note"
        className="rounded-lg border border-border bg-surface-muted p-4 text-xs text-text-muted"
      >
        <p className="font-medium text-text">Schema drift cannot arm (CH-V07)</p>
        <p className="mt-1">
          Every business subject is at its highest registered version — drift has no
          next version to inject. Register or upgrade past the current effective version
          to enable it.
        </p>
        <ul className="mt-2 space-y-0.5">
          {menu.map((m) => (
            <li key={m.subject}>
              <code className="font-mono text-text">{m.subject}</code> — effective v
              {m.effective}, highest registered {m.latest == null ? '—' : `v${String(m.latest)}`}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  const armable = menu.filter((m) => m.nextVersion != null);
  return (
    <div
      role="note"
      className="rounded-lg border border-status-green/30 bg-status-green/5 p-4 text-xs text-text-muted"
    >
      <p className="font-medium text-text">Schema drift can arm — injectable fields</p>
      <ul className="mt-2 space-y-1.5">
        {armable.map((m) => (
          <li key={m.subject} className="flex flex-wrap items-center gap-1.5">
            <code className="font-mono text-text">{m.subject}</code>
            <span>v{m.nextVersion} adds</span>
            {m.addedFields.length === 0 ? (
              <span className="italic">(no added fields)</span>
            ) : (
              m.addedFields.map((f) => (
                <span
                  key={f.path}
                  className="rounded bg-status-green/15 px-1.5 py-0.5 font-mono text-[11px] font-medium text-status-green"
                >
                  {f.path}
                </span>
              ))
            )}
          </li>
        ))}
      </ul>
      <p className="mt-2">
        Drift injects these next-version fields into delivered payloads (type-synthesized
        from the chaos sub-seed; never into cdc.before). The envelope `schema_ref` stays at
        the effective version.
      </p>
    </div>
  );
}
