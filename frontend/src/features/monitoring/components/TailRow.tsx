import { cn } from '../../../shared/lib/cn';
import type { DeliveredEnvelope } from '../../../shared/ws';

export interface TailRowProps {
  event: DeliveredEnvelope;
  expanded: boolean;
  onToggle: () => void;
}

function str(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function readField(event: DeliveredEnvelope, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const v = str(event[key]);
    if (v != null) return v;
  }
  return undefined;
}

/**
 * CDC op → chip color (event-model §4: c create / u update / d delete / r snapshot).
 * Distinct hues so a CDC stream is scannable at a glance (frontend-architecture §13).
 */
const OP_CHIP: Record<string, string> = {
  c: 'bg-status-green/20 text-status-green',
  u: 'bg-status-amber/20 text-status-amber',
  d: 'bg-status-red/20 text-status-red',
  r: 'bg-status-blue/20 text-status-blue',
};

const OP_LABEL: Record<string, string> = {
  c: 'CDC create',
  u: 'CDC update',
  d: 'CDC delete',
  r: 'CDC snapshot read',
};

/** Format an ISO occurred_at to a compact wall-clock time for the row. */
function shortTime(iso: string | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString('en-US', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');
}

/**
 * One event row in the tail (frontend-architecture §9.7 `LiveTail`). Shows
 * occurred_at, an event_type chip (CDC rows append an `op` chip), sequence_no, and
 * actor. Click toggles the JsonViewer expansion (rendered by the parent).
 */
export function TailRow({ event, expanded, onToggle }: TailRowProps) {
  const occurredAt = readField(event, 'occurred_at', 'event_time');
  const eventType = readField(event, 'event_type') ?? 'event';
  const op = readField(event, 'op'); // CDC operation: c/u/d/r
  const seqRaw = event['sequence_no'];
  const sequenceNo =
    typeof seqRaw === 'number' || typeof seqRaw === 'string' ? String(seqRaw) : null;
  const actor = readField(event, 'actor_id', 'actor', 'user_id');

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={expanded}
      data-testid="tail-row"
      data-event-type={eventType}
      className={cn(
        'flex w-full items-center gap-3 border-b border-border px-3 py-1.5 text-left text-xs hover:bg-surface-muted focus-visible:bg-surface-muted focus-visible:outline-none',
        expanded && 'bg-surface-muted',
      )}
    >
      <span className="shrink-0 font-mono tabular-nums text-text-muted" style={{ minWidth: '7.5rem' }}>
        {shortTime(occurredAt)}
      </span>
      <span className="inline-flex shrink-0 items-center gap-1 rounded bg-status-blue/15 px-1.5 py-0.5 font-medium text-status-blue">
        {eventType}
        {op && (
          <span
            data-testid="cdc-op-chip"
            data-op={op}
            className={cn(
              'rounded px-1 text-[10px] font-semibold uppercase',
              OP_CHIP[op] ?? 'bg-current/15',
            )}
            title={OP_LABEL[op] ?? 'CDC operation'}
          >
            {op}
          </span>
        )}
      </span>
      <span className="shrink-0 font-mono tabular-nums text-text-muted">
        #{sequenceNo ?? '—'}
      </span>
      <span className="min-w-0 truncate text-text-muted">{actor ?? ''}</span>
      <span aria-hidden className="ml-auto shrink-0 text-text-muted">
        {expanded ? '▾' : '▸'}
      </span>
    </button>
  );
}
