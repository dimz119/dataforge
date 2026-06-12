/**
 * Formats an events-per-second rate for display on stats cards and the live tail.
 * Sub-10 rates keep one decimal; larger rates round and group thousands.
 */
export function formatTps(eps: number): string {
  if (!Number.isFinite(eps) || eps < 0) {
    return '— TPS';
  }
  const rounded = eps >= 10 ? Math.round(eps) : Math.round(eps * 10) / 10;
  return `${rounded.toLocaleString('en-US')} TPS`;
}
