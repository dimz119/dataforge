/**
 * Simulated-time projection for the schedule panel (frontend-architecture §9.5;
 * event-model §3.5). A scheduled schema upgrade's `at` is SIMULATED time (the
 * occurred_at domain), NOT wall time — the cutover fires when the stream's virtual
 * clock reaches `at`, so a 60× stream reaches "simulated midnight" 60× sooner in wall
 * time. These helpers project the stream's virtual clock forward so the countdown
 * reads in simulated time and, separately, estimates the wall-clock ETA from the live
 * `speed_multiplier`.
 *
 * The virtual clock is sampled from the stream resource (`virtual_now` +
 * `speed_multiplier`); we advance the sample locally between polls so the countdown
 * ticks smoothly without hammering the API.
 */

export interface VirtualClockSample {
  /** The simulated "now" at the moment the stream resource was fetched (ISO-8601). */
  virtualNowIso: string | null;
  /** Virtual-clock speed multiplier (simulated time advances this many × wall time). */
  speedMultiplier: number;
  /** Wall-clock ms when the sample was taken (`Date.now()` at fetch). */
  sampledAtMs: number;
}

/**
 * The live simulated-now in ms, advancing the last sample by elapsed wall time ×
 * speed. Returns null when the clock has never started (no `virtual_now`).
 */
export function projectVirtualNowMs(sample: VirtualClockSample, atWallMs = Date.now()): number | null {
  if (!sample.virtualNowIso) return null;
  const base = new Date(sample.virtualNowIso).getTime();
  if (Number.isNaN(base)) return null;
  const elapsedWall = Math.max(0, atWallMs - sample.sampledAtMs);
  return base + elapsedWall * sample.speedMultiplier;
}

export interface CutoverCountdown {
  /** Simulated ms remaining until `at` (negative ⇒ the simulated instant has passed). */
  simulatedMsRemaining: number;
  /** Estimated wall-clock ms until the cutover, from the live speed (null if stopped). */
  wallMsRemaining: number | null;
  /** True once the simulated clock has reached/passed `at` (cutover imminent/applied). */
  due: boolean;
}

/**
 * Compute the simulated + estimated-wall countdown from the projected virtual-now to a
 * scheduled `at` (simulated). When the clock is not running (`virtual_now` null), the
 * countdown is unknown — `simulatedMsRemaining` is NaN and `wallMsRemaining` null.
 */
export function cutoverCountdown(
  atIso: string,
  sample: VirtualClockSample,
  atWallMs = Date.now(),
): CutoverCountdown {
  const at = new Date(atIso).getTime();
  const virtualNow = projectVirtualNowMs(sample, atWallMs);
  if (Number.isNaN(at) || virtualNow == null) {
    return { simulatedMsRemaining: Number.NaN, wallMsRemaining: null, due: false };
  }
  const simulatedMsRemaining = at - virtualNow;
  const speed = sample.speedMultiplier > 0 ? sample.speedMultiplier : 1;
  return {
    simulatedMsRemaining,
    wallMsRemaining: simulatedMsRemaining > 0 ? simulatedMsRemaining / speed : 0,
    due: simulatedMsRemaining <= 0,
  };
}

/** Format a positive ms duration as a compact `1d 2h 3m`/`4m 5s`/`6s` string. */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return '—';
  const total = Math.max(0, Math.round(ms / 1000));
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (days > 0) return `${String(days)}d ${String(hours)}h ${String(minutes)}m`;
  if (hours > 0) return `${String(hours)}h ${String(minutes)}m`;
  if (minutes > 0) return `${String(minutes)}m ${String(seconds)}s`;
  return `${String(seconds)}s`;
}
