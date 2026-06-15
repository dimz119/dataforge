import { useId } from 'react';

import { cn } from '../lib/cn';

export interface SparklineProps {
  /** Ordered samples (oldest first); rendered left → right. */
  values: number[];
  width?: number;
  height?: number;
  className?: string;
  /** Accessible summary, e.g. "observed TPS, last 15 minutes". */
  label?: string;
}

/**
 * A tiny dependency-free trend line (frontend-architecture §8 `Sparkline`). Used on
 * dashboard `StreamStatsCard` for observed-TPS history. Pure SVG; the polyline is
 * normalized to the value range so a flat series renders as a baseline. Decorative by
 * default (`aria-hidden`) — the numeric value beside it carries the data for SRs.
 */
export function Sparkline({
  values,
  width = 120,
  height = 32,
  className,
  label,
}: SparklineProps) {
  const gradientId = useId();
  const pad = 2;
  const usableValues = values.filter((v) => Number.isFinite(v));

  if (usableValues.length < 2) {
    return (
      <svg
        width={width}
        height={height}
        className={cn('text-status-blue', className)}
        aria-hidden={label ? undefined : true}
        role={label ? 'img' : undefined}
        aria-label={label}
      >
        <line
          x1={pad}
          y1={height - pad}
          x2={width - pad}
          y2={height - pad}
          stroke="currentColor"
          strokeWidth={1.5}
          strokeOpacity={0.4}
        />
      </svg>
    );
  }

  const max = Math.max(...usableValues);
  const min = Math.min(...usableValues);
  const range = max - min || 1;
  const stepX = (width - pad * 2) / (usableValues.length - 1);

  const points = usableValues.map((v, i) => {
    const x = pad + i * stepX;
    const y = height - pad - ((v - min) / range) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const linePath = points.join(' ');
  const areaPath = `${pad},${height - pad} ${linePath} ${(width - pad).toFixed(1)},${height - pad}`;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={cn('text-status-blue', className)}
      role={label ? 'img' : undefined}
      aria-hidden={label ? undefined : true}
      aria-label={label}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity={0.25} />
          <stop offset="100%" stopColor="currentColor" stopOpacity={0} />
        </linearGradient>
      </defs>
      <polygon points={areaPath} fill={`url(#${gradientId})`} />
      <polyline
        points={linePath}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
