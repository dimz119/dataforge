import { describe, expect, it } from 'vitest';

import { mapValidationProblem } from './formErrors';
import { ApiError } from './problem';

const base = 'https://docs.dataforge.dev/problems';

function validationError(errors: { pointer: string; detail: string }[]): ApiError {
  return new ApiError({
    status: 400,
    type: `${base}/validation-error`,
    title: 'Request validation failed',
    detail: '1 invalid field.',
    errors,
  });
}

describe('mapValidationProblem (§10.4)', () => {
  it('maps a pointer onto a known field by its last segment', () => {
    const mapped = mapValidationProblem(
      validationError([{ pointer: '#/password', detail: 'too short' }]),
      ['email', 'password'],
    );
    expect(mapped.fields.password).toBe('too short');
    expect(mapped.formLevel).toHaveLength(0);
  });

  it('routes pointers with no matching field to the form-level banner', () => {
    const mapped = mapValidationProblem(
      validationError([{ pointer: '#/captcha', detail: 'failed challenge' }]),
      ['email', 'password'],
    );
    expect(mapped.fields).toEqual({});
    expect(mapped.formLevel).toContain('failed challenge');
  });

  it('falls back to detail/title for non-validation problems (e.g. a 409 conflict)', () => {
    const conflict = new ApiError({
      status: 409,
      type: `${base}/conflict`,
      title: 'Conflict',
      detail: 'That email is already registered.',
    });
    const mapped = mapValidationProblem(conflict, ['email']);
    expect(mapped.formLevel).toEqual(['That email is already registered.']);
  });

  it('surfaces a plain Error message at the form level', () => {
    const mapped = mapValidationProblem(new Error('boom'), ['email']);
    expect(mapped.formLevel).toEqual(['boom']);
  });

  it('ignores a non-Error/non-ApiError value', () => {
    const mapped = mapValidationProblem('weird', ['email']);
    expect(mapped.fields).toEqual({});
    expect(mapped.formLevel).toEqual([]);
  });

  it('falls back to detail when a validation-error has no mappable entries', () => {
    const empty = new ApiError({
      status: 400,
      type: `${base}/validation-error`,
      title: 'Bad request',
      detail: 'Something is off.',
      errors: [],
    });
    const mapped = mapValidationProblem(empty, ['email']);
    expect(mapped.formLevel).toEqual(['Something is off.']);
  });
});
