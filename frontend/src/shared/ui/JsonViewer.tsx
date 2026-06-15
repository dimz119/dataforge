import { useState } from 'react';

import { cn } from '../lib/cn';

export interface JsonViewerProps {
  value: unknown;
  /** Initial open depth; deeper nodes start collapsed. */
  defaultExpandDepth?: number;
  className?: string;
}

/**
 * Collapsible JSON tree (frontend-architecture §8, §6.1). CRITICAL XSS posture:
 * every key and value is rendered as a TEXT NODE only — event payloads and
 * manifest literals are attacker-controlled content (threat T-8). No
 * `dangerouslySetInnerHTML`, no string interpolation into markup.
 */
export function JsonViewer({ value, defaultExpandDepth = 1, className }: JsonViewerProps) {
  return (
    <div className={cn('rounded-md bg-surface-muted p-3 font-mono text-xs text-text', className)}>
      <Node label={undefined} value={value} depth={0} defaultExpandDepth={defaultExpandDepth} />
    </div>
  );
}

interface NodeProps {
  label: string | undefined;
  value: unknown;
  depth: number;
  defaultExpandDepth: number;
}

function Node({ label, value, depth, defaultExpandDepth }: NodeProps) {
  const isObject = value !== null && typeof value === 'object';
  const [open, setOpen] = useState(depth < defaultExpandDepth);

  if (!isObject) {
    return (
      <div style={{ paddingLeft: depth * 12 }}>
        {label !== undefined && <span className="text-status-blue">{label}: </span>}
        <Scalar value={value} />
      </div>
    );
  }

  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v])
    : Object.entries(value as Record<string, unknown>);
  const bracket = Array.isArray(value) ? ['[', ']'] : ['{', '}'];

  return (
    <div style={{ paddingLeft: depth * 12 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="cursor-pointer text-left text-text hover:text-accent"
      >
        <span aria-hidden="true">{open ? '▾' : '▸'} </span>
        {label !== undefined && <span className="text-status-blue">{label}: </span>}
        <span className="text-text-muted">
          {bracket[0]}
          {!open && ` ${String(entries.length)} ${bracket[1]}`}
        </span>
      </button>
      {open && (
        <>
          {entries.map(([k, v]) => (
            <Node
              key={k}
              label={k}
              value={v}
              depth={depth + 1}
              defaultExpandDepth={defaultExpandDepth}
            />
          ))}
          <div style={{ paddingLeft: 0 }} className="text-text-muted">
            {bracket[1]}
          </div>
        </>
      )}
    </div>
  );
}

function Scalar({ value }: { value: unknown }) {
  if (typeof value === 'string') return <span className="text-status-green">&quot;{value}&quot;</span>;
  if (typeof value === 'number') return <span className="text-status-amber">{value}</span>;
  if (typeof value === 'boolean') return <span className="text-status-red">{String(value)}</span>;
  return <span className="text-text-muted">null</span>;
}
