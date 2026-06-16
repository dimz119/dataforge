/**
 * Per-stream TPS caps by plan (PRD §7, surfaced in the §9.5 TpsSlider hard-clamp).
 * The API is authoritative — a request above the cap is rejected with a 403
 * quota-exceeded at command time — so this is a UX clamp, not the enforcement point.
 * Quota meters and self-serve plan changes are Phase 11; here we only read the cap.
 */
import { TPS_MAX } from './tpsScale';

const PER_STREAM_TPS_CAP: Record<string, number> = {
  free: 50,
  classroom: 100,
  pro: 1000,
};

/** Resolve the per-stream TPS cap for a plan string, defaulting to the hard max. */
export function perStreamTpsCap(plan: string | undefined): number {
  if (plan == null) return TPS_MAX;
  return PER_STREAM_TPS_CAP[plan.toLowerCase()] ?? TPS_MAX;
}

/**
 * The virtual-clock speed-multiplier bounds (api-spec §4.8: `speed_multiplier`
 * ∈ [1, 1000] in the console — the engine accepts [0.1, 1000.0] but the console
 * unlocks faster-than-live only, Phase 8 frontend-architecture §13). UX clamp; the
 * API is the enforcement point.
 */
export const SPEED_MULTIPLIER_MIN = 1;
export const SPEED_MULTIPLIER_MAX = 1000;

/** Backfill simulated-day caps by plan (PRD §7 / api-spec §4.10: Free 7 / Classroom 30 / Pro 90). */
const BACKFILL_DAYS_CAP: Record<string, number> = {
  free: 7,
  classroom: 30,
  pro: 90,
};

/** The maximum backfill window (simulated days) for a plan; defaults to the Free cap. */
export function backfillDaysCap(plan: string | undefined): number {
  if (plan == null) return BACKFILL_DAYS_CAP.free;
  return BACKFILL_DAYS_CAP[plan.toLowerCase()] ?? BACKFILL_DAYS_CAP.free;
}
