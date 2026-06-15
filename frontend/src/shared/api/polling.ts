/**
 * Polling intervals (frontend-architecture §4.4). TanStack Query stops polling
 * automatically when the tab is hidden (`refetchIntervalInBackground: false`
 * default), so these are wall-time intervals only while the tab is visible.
 */

/** Stream statuses that are mid-transition and warrant 2 s convergence polling. */
const TRANSITIONAL = new Set<string>(['starting', 'pausing', 'resuming', 'stopping']);

/** Statuses where polling stops entirely — nothing converges. */
const SETTLED = new Set<string>(['stopped', 'failed', 'created']);

/** Convergence poll while a lifecycle command settles (§4.4). */
export const POLL_CONVERGENCE_MS = 2_000;
/** Steady-state stream-detail poll while running. */
export const POLL_RUNNING_MS = 10_000;
/** Stats poll while a monitor/control page is mounted (INV-OBS-2 ≤ 5 s). */
export const POLL_STATS_MS = 5_000;
/** Dashboard aggregate cards. */
export const POLL_DASHBOARD_MS = 15_000;
/** Stream-list status badges. */
export const POLL_STREAM_LIST_MS = 30_000;

/**
 * `refetchInterval` for stream DETAIL keyed on the current status (§4.4):
 *  - transitional → 2 s (convergence)
 *  - running → 10 s
 *  - settled (stopped/failed/created) → off (`false`)
 */
export function streamDetailInterval(status: string | undefined): number | false {
  if (status == null) return POLL_CONVERGENCE_MS;
  if (TRANSITIONAL.has(status)) return POLL_CONVERGENCE_MS;
  if (SETTLED.has(status)) return false;
  return POLL_RUNNING_MS; // running / paused* — slow steady poll
}

export function isTransitional(status: string | undefined): boolean {
  return status != null && TRANSITIONAL.has(status);
}

export function isSettled(status: string | undefined): boolean {
  return status != null && SETTLED.has(status);
}
