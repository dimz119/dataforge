import { cn } from '../lib/cn';

/**
 * A used/limit quota meter bar (frontend-architecture §9.2 / Phase 11). Renders a
 * labelled progress bar whose fill colour escalates with consumption: neutral under
 * 75 %, amber 75–99 %, red at 100 % (exhausted). The bar is an ARIA `progressbar`
 * with `aria-valuenow/min/max` so the meter is screen-reader legible.
 *
 * The console is a UX surface only — the API is the enforcement point (a stream
 * transitions to `paused_quota` server-side at command/tick time). This meter just
 * mirrors the live `used` vs `limit` numbers the quota endpoint reports.
 */
export interface QuotaMeterProps {
  label: string;
  used: number;
  limit: number;
  /** Optional unit suffix appended to the numeric readout (e.g. "TPS"). */
  unit?: string;
  className?: string;
}

/** Format a count with thousands grouping; `0` and large numbers both read cleanly. */
function fmt(n: number): string {
  return Math.max(0, Math.round(n)).toLocaleString('en-US');
}

export function QuotaMeter({ label, used, limit, unit, className }: QuotaMeterProps) {
  // A non-positive limit means "unlimited / not metered" → show usage, no bar fill.
  const hasLimit = Number.isFinite(limit) && limit > 0;
  const fraction = hasLimit ? Math.min(1, Math.max(0, used / limit)) : 0;
  const pct = Math.round(fraction * 100);
  const exhausted = hasLimit && used >= limit;
  const nearLimit = hasLimit && fraction >= 0.75 && !exhausted;

  const fillClass = exhausted
    ? 'bg-status-red'
    : nearLimit
      ? 'bg-status-amber'
      : 'bg-accent';

  const suffix = unit ? ` ${unit}` : '';
  const readout = hasLimit
    ? `${fmt(used)} / ${fmt(limit)}${suffix}`
    : `${fmt(used)}${suffix}`;

  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-text-muted">
          {label}
        </span>
        <span
          className={cn(
            'text-xs tabular-nums',
            exhausted ? 'font-semibold text-status-red' : 'text-text-muted',
          )}
        >
          {readout}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuenow={hasLimit ? pct : undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={readout}
        data-exhausted={exhausted || undefined}
        className="h-2 w-full overflow-hidden rounded-full bg-surface-muted"
      >
        <div
          className={cn('h-full rounded-full transition-all', fillClass)}
          style={{ width: hasLimit ? `${pct}%` : '0%' }}
        />
      </div>
    </div>
  );
}
