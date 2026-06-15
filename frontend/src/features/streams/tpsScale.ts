/**
 * Log-scale mapping for the target_tps slider (frontend-architecture §9.5 TpsSlider:
 * "log-scale 1–1,000"). The slider works in a linear [0, 1] position; this maps it to
 * an integer TPS on a log curve so the low end (1–10) gets as much travel as the high
 * end (100–1,000). Pure functions → unit-testable and shared by the create form.
 */

/** Hard contract bounds (api-spec §4.8.2): target_tps is an integer in [1, 1000]. */
export const TPS_MIN = 1;
export const TPS_MAX = 1000;

/** Map a TPS value to a slider position in [0, 1] (clamped to [cap]). */
export function tpsToPosition(tps: number, cap: number = TPS_MAX): number {
  const hi = Math.max(TPS_MIN, Math.min(cap, TPS_MAX));
  const clamped = Math.max(TPS_MIN, Math.min(tps, hi));
  return Math.log(clamped) / Math.log(hi);
}

/** Map a slider position in [0, 1] back to an integer TPS, clamped to the plan cap. */
export function positionToTps(position: number, cap: number = TPS_MAX): number {
  const hi = Math.max(TPS_MIN, Math.min(cap, TPS_MAX));
  const p = Math.max(0, Math.min(1, position));
  const value = Math.exp(p * Math.log(hi));
  return Math.max(TPS_MIN, Math.min(hi, Math.round(value)));
}

/** Clamp an arbitrary TPS to the contract bounds and the plan cap. */
export function clampTps(tps: number, cap: number = TPS_MAX): number {
  const hi = Math.max(TPS_MIN, Math.min(cap, TPS_MAX));
  if (!Number.isFinite(tps)) return TPS_MIN;
  return Math.max(TPS_MIN, Math.min(hi, Math.round(tps)));
}
