/**
 * The normative stream-control button-enablement matrix (frontend-architecture
 * §9.5). This is the single source of truth for which lifecycle controls render
 * enabled / pending / disabled in each status, and the slider/delete gates.
 *
 * Commands are idempotent (INV-STR-3) so double-clicks are harmless; a control in
 * `pending` reflects that the desired-state POST is in flight and the StatusBadge
 * is tracking convergence via 2 s polling (§4.4). Exported so the StreamDetailPage,
 * the LifecycleButtons, and the control-panel tests all consume one definition.
 */

/** The surfaced lifecycle string (api-spec §4.8 / domain model §4.3). */
export type StreamStatus =
  | 'created'
  | 'starting'
  | 'running'
  | 'pausing'
  | 'paused'
  | 'paused_quota'
  | 'paused_idle'
  | 'resuming'
  | 'stopping'
  | 'stopped'
  | 'failed';

/** The four lifecycle verbs (api-spec §4.8.1). */
export type StreamAction = 'start' | 'pause' | 'resume' | 'stop';

/**
 * A control's state in a given status:
 *  - `enabled`  — the verb may be issued
 *  - `pending`  — the desired state is in flight; the button shows a spinner and
 *                 is disabled (the matrix cell marked "pending" in §9.5)
 *  - `disabled` — visible but not actionable, with an explanatory tooltip
 *                 (only `paused_quota` resume, the T7 guard)
 *  - `hidden`   — not applicable in this status (the "—" cells in §9.5)
 */
export type ControlState = 'enabled' | 'pending' | 'disabled' | 'hidden';

/** One row of the §9.5 matrix: the state of every control for one status. */
export interface ControlRow {
  start: ControlState;
  pause: ControlState;
  resume: ControlState;
  stop: ControlState;
  /** The log-scale TPS slider (only meaningful while `running`). */
  tps: ControlState;
  delete: ControlState;
}

/**
 * The matrix verbatim from frontend-architecture §9.5 ("Button-enablement matrix
 * (normative)"). Any change here is a spec change. `paused_quota` resume is the
 * sole `disabled` (T7 guard, tooltip below); all other non-actionable cells are
 * `hidden`.
 */
export const CONTROL_MATRIX: Record<StreamStatus, ControlRow> = {
  created: { start: 'enabled', pause: 'hidden', resume: 'hidden', stop: 'hidden', tps: 'hidden', delete: 'enabled' },
  starting: { start: 'pending', pause: 'hidden', resume: 'hidden', stop: 'enabled', tps: 'hidden', delete: 'hidden' },
  running: { start: 'hidden', pause: 'enabled', resume: 'hidden', stop: 'enabled', tps: 'enabled', delete: 'hidden' },
  pausing: { start: 'hidden', pause: 'pending', resume: 'hidden', stop: 'enabled', tps: 'hidden', delete: 'hidden' },
  paused: { start: 'hidden', pause: 'hidden', resume: 'enabled', stop: 'enabled', tps: 'hidden', delete: 'hidden' },
  paused_quota: { start: 'hidden', pause: 'hidden', resume: 'disabled', stop: 'enabled', tps: 'hidden', delete: 'hidden' },
  paused_idle: { start: 'hidden', pause: 'hidden', resume: 'enabled', stop: 'enabled', tps: 'hidden', delete: 'hidden' },
  resuming: { start: 'hidden', pause: 'hidden', resume: 'pending', stop: 'enabled', tps: 'hidden', delete: 'hidden' },
  stopping: { start: 'hidden', pause: 'hidden', resume: 'hidden', stop: 'pending', tps: 'hidden', delete: 'hidden' },
  stopped: { start: 'enabled', pause: 'hidden', resume: 'hidden', stop: 'hidden', tps: 'hidden', delete: 'enabled' },
  failed: { start: 'enabled', pause: 'hidden', resume: 'hidden', stop: 'hidden', tps: 'hidden', delete: 'enabled' },
};

/** Fallback row for an unknown status string (everything hidden — safest). */
const HIDDEN_ROW: ControlRow = {
  start: 'hidden',
  pause: 'hidden',
  resume: 'hidden',
  stop: 'hidden',
  tps: 'hidden',
  delete: 'hidden',
};

/** Resolve the control row for a status, tolerant of unknown strings. */
export function controlRow(status: string): ControlRow {
  return (CONTROL_MATRIX as Record<string, ControlRow>)[status] ?? HIDDEN_ROW;
}

/**
 * The Start button's label is context-dependent (§9.5): "Start" from `created`,
 * "Start (continues from checkpoint)" from `stopped` (T12), "Retry" from `failed`
 * (T13). The verb POSTed is always `start` (continuation/retry are server-side).
 */
export function startLabel(status: string): string {
  if (status === 'stopped') return 'Start';
  if (status === 'failed') return 'Retry';
  return 'Start';
}

/** Short helper line shown beside Start, surfacing T12/T13 continuation semantics. */
export function startHint(status: string): string | undefined {
  if (status === 'stopped') return 'Continues from checkpoint';
  if (status === 'failed') return 'Retry the failed start';
  return undefined;
}

/** The T7 tooltip for the disabled resume in `paused_quota`. */
export const QUOTA_RESUME_TOOLTIP = 'Quota headroom required to resume';

/** Maps a control key to the lifecycle verb POSTed when it is pressed. */
export const ACTION_FOR: Record<'start' | 'pause' | 'resume' | 'stop', StreamAction> = {
  start: 'start',
  pause: 'pause',
  resume: 'resume',
  stop: 'stop',
};
