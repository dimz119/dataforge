/**
 * Unit tests for the manifest-introspection overlay readers (overlay.ts;
 * frontend-architecture §9.4, overlay grammar scenario-plugin-architecture §11.1).
 *
 * These pure readers feed the instance-config editors (probability sliders clamped
 * to override bounds, dwell editors, catalog-size inputs, CDC entity toggles,
 * intensity curves). The page render paths are E2E-covered; the EXTRACTION logic
 * — the defaulting rules and the malformed-input narrowing — is unit-covered here.
 */
import { describe, expect, it } from 'vitest';

import {
  readCatalogBounds,
  readCdcEntities,
  readIntensityDefaults,
  readTransitionOverrides,
  CATALOG_SUM_CAP,
  INTENSITY_MAX,
} from './overlay';

describe('readTransitionOverrides', () => {
  const document = {
    state_machines: {
      session: {
        states: {
          browsing: {
            transitions: [
              {
                to: 'checkout_started',
                probability: 0.4,
                override: { allowed: true, min: 0.1, max: 0.8 },
                dwell: { family: 'lognormal' },
              },
              // No override block → allowed:true, bounds default [0,1].
              { to: 'exit', probability: 0.6 },
              // Locked transition (override.allowed:false) still surfaces read-only.
              { to: 'fraud_hold', probability: 0.0, override: { allowed: false } },
              // No `to` → skipped entirely.
              { probability: 0.5 },
            ],
          },
          // `transitions` not an array → state skipped without throwing.
          settled: { transitions: 'nope' },
        },
      },
    },
  };

  it('extracts keyed override bounds, dwell family, and the manifest default', () => {
    const rows = readTransitionOverrides(document);
    expect(rows).toHaveLength(3);

    const checkout = rows.find((r) => r.to === 'checkout_started');
    expect(checkout).toMatchObject({
      key: 'session.browsing.checkout_started',
      machine: 'session',
      state: 'browsing',
      default: 0.4,
      allowed: true,
      min: 0.1,
      max: 0.8,
      dwellFamily: 'lognormal',
    });
  });

  it('defaults allowed:true and bounds [0,1] when there is no override block', () => {
    const exit = readTransitionOverrides(document).find((r) => r.to === 'exit');
    expect(exit).toMatchObject({ allowed: true, min: 0, max: 1, dwellFamily: undefined });
  });

  it('surfaces a locked transition as allowed:false (read-only slider)', () => {
    const locked = readTransitionOverrides(document).find((r) => r.to === 'fraud_hold');
    expect(locked?.allowed).toBe(false);
  });

  it('returns [] for a document with no state_machines', () => {
    expect(readTransitionOverrides({})).toEqual([]);
    expect(readTransitionOverrides({ state_machines: null })).toEqual([]);
  });
});

describe('readCatalogBounds', () => {
  it('reads default/min/max with a 100k max fallback', () => {
    const bounds = readCatalogBounds({
      seeding: {
        catalogs: {
          users: { default: 1000, min: 100, max: 50_000 },
          products: { default: 500 }, // min→0, max→100_000 defaults
        },
      },
    });
    expect(bounds).toEqual([
      { entity: 'users', default: 1000, min: 100, max: 50_000 },
      { entity: 'products', default: 500, min: 0, max: 100_000 },
    ]);
  });

  it('returns [] when seeding.catalogs is absent', () => {
    expect(readCatalogBounds({})).toEqual([]);
  });

  it('exposes the B-08 sum cap constant', () => {
    expect(CATALOG_SUM_CAP).toBe(250_000);
  });
});

describe('readCdcEntities', () => {
  it('reads entities with enabled_default coerced to a strict boolean', () => {
    const entities = readCdcEntities({
      cdc: {
        entities: {
          orders: { enabled_default: true },
          inventory: { enabled_default: false },
          users: {}, // missing → false
        },
      },
    });
    expect(entities).toEqual([
      { entity: 'orders', enabledDefault: true },
      { entity: 'inventory', enabledDefault: false },
      { entity: 'users', enabledDefault: false },
    ]);
  });

  it('returns [] when cdc.entities is absent', () => {
    expect(readCdcEntities({})).toEqual([]);
  });
});

describe('readIntensityDefaults', () => {
  it('reads diurnal buckets and the weekly curve with multiplier defaults', () => {
    const { diurnal, weekly } = readIntensityDefaults({
      intensity: {
        diurnal: [
          { from_hour: 0, to_hour: 6, multiplier: 0.3 },
          { from_hour: 18, to_hour: 22 }, // multiplier → 1 default
        ],
        weekly: { mon: 1.2, sun: 'bad' }, // non-number → 1 default
      },
    });
    expect(diurnal).toEqual([
      { from_hour: 0, to_hour: 6, multiplier: 0.3 },
      { from_hour: 18, to_hour: 22, multiplier: 1 },
    ]);
    expect(weekly).toEqual({ mon: 1.2, sun: 1 });
  });

  it('returns empty curves when intensity is absent or malformed', () => {
    expect(readIntensityDefaults({})).toEqual({ diurnal: [], weekly: {} });
    expect(readIntensityDefaults({ intensity: { diurnal: 'nope' } })).toEqual({
      diurnal: [],
      weekly: {},
    });
  });

  it('exposes the B-15 intensity max constant', () => {
    expect(INTENSITY_MAX).toBe(10);
  });
});
