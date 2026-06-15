/**
 * OverlayErrorMap (frontend-architecture §9.4): map MAN-V* manifest-validation errors
 * from a 422 onto the exact overlay control. The backend returns
 * `manifest-validation-failed` problems whose `errors[]` carry a JSON Pointer `path`
 * into the canonical merged document (scenario-plugin-architecture §10) plus the
 * MAN-V code and (for sum/bound errors) `bound`/`actual` numbers.
 *
 * Examples (api-spec §2.7.4):
 *  - MAN-V201 sum: path `/state_machines/{machine}/states/{state}` →
 *    highlights that state's slider GROUP, "probabilities sum to 1.15; must be ≤ 1.0"
 *  - MAN-V208 bound: path `/state_machines/{machine}/states/{state}/transitions/{i}` →
 *    a single transition's slider
 *  - catalog/intensity/cdc bounds: path `/seeding/catalogs/{entity}`, `/intensity/…`,
 *    `/cdc/entities/{entity}` → the matching control
 */
import { ApiError, type ProblemFieldError } from '../../shared/api/problem';

/** A located overlay error: which control + the message + numeric bound/actual. */
export interface OverlayError {
  message: string;
  code?: string;
  bound?: number;
  actual?: number;
}

/**
 * The map keyed by a STABLE control id we also use as the slider/group/input id:
 *  - `state:{machine}.{state}`        → the slider GROUP for a state (sum errors)
 *  - `transition:{machine}.{state}.{to}` → one transition's slider (bound errors)
 *  - `catalog:{entity}`               → a catalog size input
 *  - `cdc:{entity}`                   → a CDC toggle
 *  - `intensity`                      → the intensity editor
 *  - `chaos:{mode}`                   → a chaos-defaults control
 *  - `form`                           → unlocated errors → the page-level banner
 */
export type OverlayErrorMap = Record<string, OverlayError[]>;

/** Parse a JSON Pointer like `/state_machines/m/states/s/transitions/0` into segments. */
function segments(pointer: string): string[] {
  return pointer.split('/').filter(Boolean).map((s) => s.replace(/~1/g, '/').replace(/~0/g, '~'));
}

/** Locate one error onto a control id (returns 'form' when unlocatable). */
export function locateOverlayError(err: ProblemFieldError): string {
  const seg = segments(err.pointer);
  if (seg.length === 0) return 'form';

  if (seg[0] === 'state_machines' && seg[2] === 'states') {
    const machine = seg[1];
    const state = seg[3];
    if (seg[4] === 'transitions' && seg[6] != null) {
      // points at a specific transition by index — but the overlay key is by `to`,
      // which the index does not give us; fall back to the state group so the whole
      // group highlights (still pinpoints the right state's sliders).
      return `state:${machine}.${state}`;
    }
    if (seg[4] === 'transitions' && seg[5] != null) {
      return `state:${machine}.${state}`;
    }
    return `state:${machine}.${state}`;
  }
  if (seg[0] === 'seeding' && seg[1] === 'catalogs' && seg[2] != null) {
    return `catalog:${seg[2]}`;
  }
  if (seg[0] === 'catalog_sizes' && seg[1] != null) {
    return `catalog:${seg[1]}`;
  }
  if (seg[0] === 'cdc' && seg[1] === 'entities' && seg[2] != null) {
    return `cdc:${seg[2]}`;
  }
  if (seg[0] === 'cdc_entities') {
    return 'cdc:*';
  }
  if (seg[0] === 'intensity') return 'intensity';
  if (seg[0] === 'chaos' && seg[1] != null) return `chaos:${seg[1]}`;
  return 'form';
}

/**
 * Build the OverlayErrorMap from a thrown error. Only `manifest-validation-failed`
 * (MAN-V*) errors are mapped here; anything else returns an empty map so the caller
 * falls back to its generic handling (§10.1).
 */
export function buildOverlayErrorMap(error: unknown): OverlayErrorMap {
  const map: OverlayErrorMap = {};
  if (!(error instanceof ApiError) || error.slug !== 'manifest-validation-failed') {
    return map;
  }
  for (const e of error.errors ?? []) {
    const id = locateOverlayError(e);
    (map[id] ??= []).push({
      message: e.detail,
      code: e.code,
      bound: e.bound,
      actual: e.actual,
    });
  }
  // If nothing located, surface the problem detail at form level.
  if (Object.keys(map).length === 0 && error.detail) {
    map.form = [{ message: error.detail }];
  }
  return map;
}

/** Collect the form-level (unlocated) overlay errors for the page banner. */
export function formLevelOverlayErrors(map: OverlayErrorMap): OverlayError[] {
  return map.form ?? [];
}
