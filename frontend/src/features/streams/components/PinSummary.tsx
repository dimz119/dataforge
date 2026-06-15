import { CopyField } from '../../../shared/ui';
import type { StreamResponse } from '../../../shared/api/types';

export interface PinSummaryProps {
  stream: StreamResponse;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
      <dt className="text-sm text-text-muted">{label}</dt>
      <dd className="font-mono text-sm text-text">{children}</dd>
    </div>
  );
}

/**
 * Read-only stream pin (frontend-architecture §9.5 PinSummary). Surfaces the golden-
 * replay identity — `{scenario_slug}@{manifest_version}`, the merged-config sha256
 * (copyable, PIN-1), the seed, and the virtual-clock config — with the PIN-4 note
 * that these can only change by creating a new stream. speed_multiplier is locked to
 * 1× / live until Phase 8.
 */
export function PinSummary({ stream }: PinSummaryProps) {
  return (
    <section
      aria-labelledby="pin-heading"
      className="space-y-4 rounded-lg border border-border bg-surface p-5"
    >
      <div>
        <h2 id="pin-heading" className="text-sm font-semibold text-text">
          Pin
        </h2>
        <p className="mt-0.5 text-xs text-text-muted">
          A new stream is required to change any of these (PIN-4).
        </p>
      </div>
      <dl className="space-y-3">
        <Row label="Scenario">
          {stream.scenario_slug}@{stream.manifest_version}
        </Row>
        <Row label="Config revision">rev {stream.config_revision}</Row>
        <Row label="Seed">{stream.seed}</Row>
        <Row label="Virtual clock">
          {/* Phase 8: speed_multiplier unlocks ≠ 1× and backfill mode. */}
          {stream.virtual_clock.speed_multiplier}× · live
        </Row>
      </dl>
      <div>
        <p className="mb-1 text-sm text-text-muted">Merged-config sha256 (golden-replay id)</p>
        <CopyField value={stream.pin_sha256} label="config hash" mono />
      </div>
    </section>
  );
}
