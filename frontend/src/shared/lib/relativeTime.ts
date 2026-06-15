/**
 * Relative-time formatter for audit entries, `last_used_at`, `created_at` etc.
 * (frontend-architecture §9.3/§9.6). Uses the platform `Intl.RelativeTimeFormat`
 * so it is locale-aware with zero bundle cost (§12 budgets).
 */
const RTF = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });

const DIVISIONS: { amount: number; unit: Intl.RelativeTimeFormatUnit }[] = [
  { amount: 60, unit: 'second' },
  { amount: 60, unit: 'minute' },
  { amount: 24, unit: 'hour' },
  { amount: 7, unit: 'day' },
  { amount: 4.34524, unit: 'week' },
  { amount: 12, unit: 'month' },
  { amount: Number.POSITIVE_INFINITY, unit: 'year' },
];

/**
 * Format an ISO-8601 timestamp (or null) as e.g. "3 minutes ago" / "in 2 days".
 * Returns "never" for null/empty so callers render a stable placeholder.
 */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return 'never';
  let duration = (then - Date.now()) / 1000;
  for (const division of DIVISIONS) {
    if (Math.abs(duration) < division.amount) {
      return RTF.format(Math.round(duration), division.unit);
    }
    duration /= division.amount;
  }
  return RTF.format(Math.round(duration), 'year');
}
