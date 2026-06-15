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
