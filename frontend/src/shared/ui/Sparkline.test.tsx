import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Sparkline } from './Sparkline';

describe('Sparkline', () => {
  it('renders a baseline for fewer than two points', () => {
    const { container } = render(<Sparkline values={[5]} />);
    expect(container.querySelector('line')).not.toBeNull();
    expect(container.querySelector('polyline')).toBeNull();
  });

  it('renders a polyline for a series', () => {
    const { container } = render(<Sparkline values={[1, 5, 2, 8, 3]} label="tps" />);
    const polyline = container.querySelector('polyline');
    expect(polyline).not.toBeNull();
    // 5 points → 5 coordinate pairs.
    expect(polyline?.getAttribute('points')?.trim().split(' ')).toHaveLength(5);
  });

  it('exposes an accessible label as an image role when provided', () => {
    const { container } = render(<Sparkline values={[1, 2, 3]} label="observed TPS" />);
    const svg = container.querySelector('svg');
    expect(svg?.getAttribute('role')).toBe('img');
    expect(svg?.getAttribute('aria-label')).toBe('observed TPS');
  });

  it('tolerates non-finite values without throwing', () => {
    const { container } = render(<Sparkline values={[Number.NaN, 1, 2, Number.POSITIVE_INFINITY, 3]} />);
    expect(container.querySelector('polyline')).not.toBeNull();
  });
});
