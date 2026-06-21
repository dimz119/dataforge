import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ChaosModeConfig } from '../types';
import { ChaosModeCard } from './ChaosModeCard';

function setup(config: ChaosModeConfig) {
  const onChange = vi.fn<(next: ChaosModeConfig) => void>();
  render(<ChaosModeCard mode="duplicates" config={config} onChange={onChange} />);
  return { onChange };
}

describe('ChaosModeCard rate slider (B-16 / CH-V01 cap)', () => {
  it('renders the rate slider only when the mode is enabled', () => {
    setup({ enabled: false, rate: 0.05, params: {} });
    expect(screen.queryByRole('slider', { name: /rate/i })).not.toBeInTheDocument();
  });

  it('caps the rate slider maximum at 0.5', () => {
    setup({ enabled: true, rate: 0.5, params: {} });
    const slider = screen.getByRole('slider', { name: /Duplicates rate/i });
    expect(slider).toHaveAttribute('aria-valuemax', '0.5');
  });

  it('never emits a rate above 0.5 even when pushed past the max', () => {
    const { onChange } = setup({ enabled: true, rate: 0.5, params: {} });
    const slider = screen.getByRole('slider', { name: /Duplicates rate/i });
    slider.focus();
    // Arrow-right at the maximum must not exceed the 0.5 ceiling.
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    for (const call of onChange.mock.calls) {
      expect(call[0].rate).toBeLessThanOrEqual(0.5);
    }
  });
});
