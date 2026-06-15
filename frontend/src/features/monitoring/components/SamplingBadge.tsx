export interface SamplingBadgeProps {
  active: boolean;
  /** keepRatio = 1/k (the fraction of events displayed). */
  keepRatio: number;
}

/**
 * High-volume display-sampling signal (frontend-architecture §7.5). Counters remain
 * EXACT; only the rendered tail is sampled. Hidden when sampling is inactive.
 */
export function SamplingBadge({ active, keepRatio }: SamplingBadgeProps) {
  if (!active) return null;
  const k = Math.max(1, Math.round(1 / keepRatio));
  return (
    <span
      role="status"
      className="inline-flex items-center gap-1 rounded-full bg-status-amber/15 px-2 py-0.5 text-xs font-medium text-status-amber"
      title="Counters remain exact; only the display is sampled."
    >
      high volume — displaying 1/{k} of events
    </span>
  );
}
