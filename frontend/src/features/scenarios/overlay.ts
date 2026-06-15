/**
 * The workspace-configuration overlay shape + manifest-introspection helpers
 * (frontend-architecture §9.4; overlay grammar scenario-plugin-architecture §11.1).
 *
 * The generated client types `configuration` and the manifest `document` as opaque
 * `{[key: string]: unknown}` (they are free-form JSON the backend validates), so this
 * module defines the typed overlay we edit and the readers that pull override bounds,
 * catalog bounds, dwell families, CDC entities, and intensity defaults out of a
 * manifest document. Everything here is pure → unit-testable.
 */

/** Distribution family + parameters for a dwell editor (§9.4 DwellEditors). */
export interface DwellSpec {
  family: string;
  [param: string]: string | number;
}

/** Diurnal bucket (24-hour intensity, §9.1 shapes). */
export interface DiurnalBucket {
  from_hour: number;
  to_hour: number;
  multiplier: number;
}

/** The 7-day weekly multipliers (0–10). */
export type WeeklyCurve = Record<string, number>;

/** The overlay document we edit (overlay grammar §11.1; full replacement on save). */
export interface Overlay {
  /** keyed "machine.state.to" → probability. */
  probabilities?: Record<string, number>;
  /** same key → distribution params (family is manifest-fixed). */
  dwell?: Record<string, DwellSpec>;
  catalog_sizes?: Record<string, number>;
  intensity?: { diurnal?: DiurnalBucket[]; weekly?: WeeklyCurve };
  /** subset of manifest cdc.entities (R-CDC-M1). */
  cdc_entities?: string[];
  /** per-mode chaos defaults written to the instance (live panel is Phase 9). */
  chaos?: Record<string, { enabled?: boolean; rate?: number }>;
  simulated_timezone?: string;
}

/** Override bounds for one transition (manifest `override: {allowed, min, max}`). */
export interface TransitionOverride {
  /** "machine.state.to" — the overlay key and the JSON-Pointer leaf. */
  key: string;
  machine: string;
  state: string;
  to: string;
  /** manifest default probability (marked on the slider track). */
  default: number;
  allowed: boolean;
  min: number;
  max: number;
  dwellFamily?: string;
}

/** One catalog entity's bounds (manifest `seeding.catalogs.{entity}`). */
export interface CatalogBound {
  entity: string;
  default: number;
  min: number;
  max: number;
}

/** The B-08 sum cap across all configured catalogs. */
export const CATALOG_SUM_CAP = 250_000;

/** Narrow an unknown to a plain record. */
function obj(v: unknown): Record<string, unknown> {
  return v != null && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}

/**
 * Read every transition's override bounds from a manifest document
 * (state_machines.{machine}.states.{state}.transitions[]). Non-overridable
 * transitions (`override.allowed: false`, or no override block → default allowed
 * true with [0,1]) still surface so the sliders can render them read-only (§9.4).
 */
export function readTransitionOverrides(document: Record<string, unknown>): TransitionOverride[] {
  const out: TransitionOverride[] = [];
  const machines = obj(document['state_machines']);
  for (const [machine, mRaw] of Object.entries(machines)) {
    const states = obj(obj(mRaw)['states']);
    for (const [state, sRaw] of Object.entries(states)) {
      const transitions = obj(sRaw)['transitions'];
      if (!Array.isArray(transitions)) continue;
      for (const tRaw of transitions) {
        const t = obj(tRaw);
        const to = typeof t['to'] === 'string' ? t['to'] : '';
        if (!to) continue;
        const override = obj(t['override']);
        const hasOverride = Object.keys(override).length > 0;
        const dwell = obj(t['dwell']);
        out.push({
          key: `${machine}.${state}.${to}`,
          machine,
          state,
          to,
          default: num(t['probability'], 0),
          // Default per §11.1: allowed defaults true, bounds default [0, 1].
          allowed: hasOverride ? override['allowed'] !== false : true,
          min: num(override['min'], 0),
          max: num(override['max'], 1),
          dwellFamily: typeof dwell['family'] === 'string' ? dwell['family'] : undefined,
        });
      }
    }
  }
  return out;
}

/** Read catalog bounds from `seeding.catalogs.{entity}: {default, min, max}`. */
export function readCatalogBounds(document: Record<string, unknown>): CatalogBound[] {
  const catalogs = obj(obj(document['seeding'])['catalogs']);
  return Object.entries(catalogs).map(([entity, cRaw]) => {
    const c = obj(cRaw);
    return {
      entity,
      default: num(c['default'], 0),
      min: num(c['min'], 0),
      max: num(c['max'], 100_000),
    };
  });
}

/** Read CDC-eligible entities + their manifest enable-default (`cdc.entities`). */
export function readCdcEntities(
  document: Record<string, unknown>,
): { entity: string; enabledDefault: boolean }[] {
  const entities = obj(obj(document['cdc'])['entities']);
  return Object.entries(entities).map(([entity, eRaw]) => ({
    entity,
    enabledDefault: obj(eRaw)['enabled_default'] === true,
  }));
}

/** Read the manifest's default intensity curves (diurnal + weekly). */
export function readIntensityDefaults(document: Record<string, unknown>): {
  diurnal: DiurnalBucket[];
  weekly: WeeklyCurve;
} {
  const intensity = obj(document['intensity']);
  const diurnalRaw = intensity['diurnal'];
  const diurnal: DiurnalBucket[] = Array.isArray(diurnalRaw)
    ? diurnalRaw.map((b) => {
        const o = obj(b);
        return {
          from_hour: num(o['from_hour'], 0),
          to_hour: num(o['to_hour'], 0),
          multiplier: num(o['multiplier'], 1),
        };
      })
    : [];
  const weeklyRaw = obj(intensity['weekly']);
  const weekly: WeeklyCurve = {};
  for (const [day, m] of Object.entries(weeklyRaw)) weekly[day] = num(m, 1);
  return { diurnal, weekly };
}

/** The intensity multiplier bound (B-15: multipliers ∈ [0, 10]). */
export const INTENSITY_MAX = 10;
