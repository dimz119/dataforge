import { describe, expect, it } from 'vitest';

import { ApiError } from '../../shared/api/problem';
import { buildOverlayErrorMap, locateOverlayError } from './overlayErrors';

/** The exact MAN-V201 problem from api-spec §2.7.4 (probability sum overflow). */
function manV201(): ApiError {
  return new ApiError({
    status: 422,
    type: 'https://docs.dataforge.dev/problems/manifest-validation-failed',
    title: 'Manifest validation failed',
    detail: '1 error in semantic validation (layer 2).',
    errors: [
      {
        pointer: '/state_machines/shopping_session/states/checkout_started',
        detail: 'outgoing probabilities sum to 1.15; must be <= 1.0',
        code: 'MAN-V201',
        bound: 1.0,
        actual: 1.15,
      },
    ],
  });
}

describe('OverlayErrorMap (§9.4, JSON-Pointer → control)', () => {
  it('locates a MAN-V201 sum error onto the offending state slider GROUP', () => {
    const id = locateOverlayError({
      pointer: '/state_machines/shopping_session/states/checkout_started',
      detail: 'sum',
    });
    expect(id).toBe('state:shopping_session.checkout_started');
  });

  it('maps the MAN-V201 422 onto state:{machine}.{state} with bound/actual', () => {
    const map = buildOverlayErrorMap(manV201());
    const key = 'state:shopping_session.checkout_started';
    expect(map[key]).toBeDefined();
    const located = map[key][0];
    expect(located.code).toBe('MAN-V201');
    expect(located.bound).toBe(1.0);
    expect(located.actual).toBe(1.15);
    expect(located.message).toContain('1.15');
  });

  it('locates catalog, cdc, intensity, and chaos pointers', () => {
    expect(locateOverlayError({ pointer: '/seeding/catalogs/users', detail: '' })).toBe('catalog:users');
    expect(locateOverlayError({ pointer: '/catalog_sizes/products', detail: '' })).toBe('catalog:products');
    expect(locateOverlayError({ pointer: '/cdc/entities/orders', detail: '' })).toBe('cdc:orders');
    expect(locateOverlayError({ pointer: '/intensity/diurnal/0', detail: '' })).toBe('intensity');
    expect(locateOverlayError({ pointer: '/chaos/duplicates/rate', detail: '' })).toBe('chaos:duplicates');
  });

  it('returns an empty map for non-MAN-V errors (caller falls back to §10.1)', () => {
    const validation = new ApiError({
      status: 400,
      type: 'https://docs.dataforge.dev/problems/validation-error',
      title: 'Validation error',
    });
    expect(buildOverlayErrorMap(validation)).toEqual({});
  });

  it('surfaces an unlocatable error at form level', () => {
    const err = new ApiError({
      status: 422,
      type: 'https://docs.dataforge.dev/problems/manifest-validation-failed',
      title: 'Manifest validation failed',
      detail: 'something general',
      errors: [{ pointer: '', detail: 'no pointer' }],
    });
    const map = buildOverlayErrorMap(err);
    expect(map.form).toBeDefined();
  });
});
