/**
 * Map a `validation-error` problem (§10.1, §10.4) onto react-hook-form fields.
 * `errors[].pointer` is a JSON Pointer (`#/password`) or `#/<field>`; the last
 * non-empty segment is the field name. Pointers that do not match a known field
 * are returned as form-level messages for a banner.
 */
import { ApiError } from './problem';

export interface MappedFormErrors<FieldName extends string> {
  /** field → message (pass to `setError`). */
  fields: Partial<Record<FieldName, string>>;
  /** Messages with no matching field → form-level banner. */
  formLevel: string[];
}

export function mapValidationProblem<FieldName extends string>(
  error: unknown,
  knownFields: readonly FieldName[],
): MappedFormErrors<FieldName> {
  const result: MappedFormErrors<FieldName> = { fields: {}, formLevel: [] };
  if (!(error instanceof ApiError)) {
    if (error instanceof Error) result.formLevel.push(error.message);
    return result;
  }
  if (error.slug !== 'validation-error' || !error.errors) {
    result.formLevel.push(error.detail ?? error.title);
    return result;
  }
  for (const fieldError of error.errors) {
    const segment = fieldError.pointer.split('/').filter(Boolean).pop() ?? '';
    const match = knownFields.find((f) => f === segment);
    if (match) {
      result.fields[match] = fieldError.detail;
    } else {
      result.formLevel.push(fieldError.detail);
    }
  }
  if (result.formLevel.length === 0 && Object.keys(result.fields).length === 0) {
    result.formLevel.push(error.detail ?? error.title);
  }
  return result;
}
