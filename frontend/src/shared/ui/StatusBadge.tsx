import { cn } from '../lib/cn';

/**
 * The single rendering of the surfaced stream status (frontend-architecture §8).
 * Used identically on dashboard cards, list rows, control panel, and monitor
 * header. The color token + pulse animation are NORMATIVE per the §8 table;
 * `paused_quota`/`paused_idle` append a reason chip.
 */
type Tone = 'gray' | 'blue' | 'green' | 'amber' | 'red';

interface StatusSpec {
  tone: Tone;
  pulse: boolean;
  reason?: string;
}

const STATUS: Record<string, StatusSpec> = {
  created: { tone: 'gray', pulse: false },
  starting: { tone: 'blue', pulse: true },
  resuming: { tone: 'blue', pulse: true },
  running: { tone: 'green', pulse: false },
  pausing: { tone: 'amber', pulse: true },
  stopping: { tone: 'amber', pulse: true },
  paused: { tone: 'amber', pulse: false },
  paused_quota: { tone: 'amber', pulse: false, reason: 'quota' },
  paused_idle: { tone: 'amber', pulse: false, reason: 'idle' },
  stopped: { tone: 'gray', pulse: false },
  failed: { tone: 'red', pulse: false },
};

const TONE_CLASS: Record<Tone, string> = {
  gray: 'bg-status-gray/15 text-status-gray',
  blue: 'bg-status-blue/15 text-status-blue',
  green: 'bg-status-green/15 text-status-green',
  amber: 'bg-status-amber/15 text-status-amber',
  red: 'bg-status-red/15 text-status-red',
};

const DOT_CLASS: Record<Tone, string> = {
  gray: 'bg-status-gray',
  blue: 'bg-status-blue',
  green: 'bg-status-green',
  amber: 'bg-status-amber',
  red: 'bg-status-red',
};

export interface StatusBadgeProps {
  status: string;
  className?: string;
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  const spec = STATUS[status] ?? { tone: 'gray' as const, pulse: false };
  const label = status.replace(/_/g, ' ');
  return (
    <span
      role="status"
      data-testid="status-badge"
      data-status={status}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium capitalize',
        TONE_CLASS[spec.tone],
        className,
      )}
    >
      <span className={cn('h-1.5 w-1.5 rounded-full', DOT_CLASS[spec.tone], spec.pulse && 'df-pulse')} />
      {label}
      {spec.reason && (
        <span className="rounded bg-current/10 px-1 text-[10px] font-semibold not-italic">
          {spec.reason}
        </span>
      )}
    </span>
  );
}
