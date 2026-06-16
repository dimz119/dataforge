import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { VirtualClockSection, type VirtualClockValue } from './VirtualClockSection';

const base: VirtualClockValue = { speedMultiplier: 1, mode: 'live', backfillDays: 7 };

describe('VirtualClockSection (Phase 8 virtual-clock controls)', () => {
  it('unlocks the speed multiplier — a preset sets it (faster-than-live)', async () => {
    const onChange = vi.fn();
    render(<VirtualClockSection value={base} onChange={onChange} />);
    await userEvent.click(screen.getByRole('button', { name: '60×' }));
    expect(onChange).toHaveBeenCalledWith({ ...base, speedMultiplier: 60 });
  });

  it('the slider drives the multiplier within [1,1000]', () => {
    const onChange = vi.fn();
    render(<VirtualClockSection value={base} onChange={onChange} />);
    const slider = screen.getByRole('slider', { name: /speed multiplier/i });
    expect(slider).toHaveAttribute('min', '1');
    expect(slider).toHaveAttribute('max', '1000');
  });

  it('selecting backfill reveals the backfill-days input clamped to the plan cap', async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <VirtualClockSection value={base} onChange={onChange} plan="free" />,
    );
    // No backfill-days field in live mode.
    expect(screen.queryByLabelText(/backfill days/i)).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByRole('combobox', { name: /mode/i }), 'backfill');
    expect(onChange).toHaveBeenCalledWith({ ...base, mode: 'backfill' });

    rerender(
      <VirtualClockSection
        value={{ ...base, mode: 'backfill' }}
        onChange={onChange}
        plan="free"
      />,
    );
    const days = screen.getByLabelText(/backfill days/i);
    expect(days).toHaveAttribute('max', '7'); // Free plan cap (PRD §7)
    // The datasets-resource note appears in backfill mode.
    expect(screen.getByText(/downloadable dataset/i)).toBeInTheDocument();
  });

  it('uses the Pro plan cap of 90 backfill days', () => {
    render(
      <VirtualClockSection value={{ ...base, mode: 'backfill' }} onChange={() => {}} plan="pro" />,
    );
    expect(screen.getByLabelText(/backfill days/i)).toHaveAttribute('max', '90');
  });
});
