import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { QuotaMeter } from './QuotaMeter';

describe('QuotaMeter', () => {
  it('renders a progressbar with the used/limit readout and percentage', () => {
    render(<QuotaMeter label="Events / day" used={250_000} limit={1_000_000} />);
    const bar = screen.getByRole('progressbar', { name: 'Events / day' });
    expect(bar).toHaveAttribute('aria-valuenow', '25');
    expect(bar).toHaveAttribute('aria-valuemin', '0');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
    expect(screen.getByText('250,000 / 1,000,000')).toBeInTheDocument();
  });

  it('appends the unit suffix when provided', () => {
    render(<QuotaMeter label="Aggregate TPS" used={40} limit={100} unit="TPS" />);
    expect(screen.getByText('40 / 100 TPS')).toBeInTheDocument();
  });

  it('marks the bar exhausted at or above the limit', () => {
    render(<QuotaMeter label="Concurrent streams" used={5} limit={5} />);
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('data-exhausted', 'true');
    expect(bar).toHaveAttribute('aria-valuenow', '100');
  });

  it('clamps overshoot to 100% (used > limit cannot exceed the bar)', () => {
    render(<QuotaMeter label="Events / day" used={1_200_000} limit={1_000_000} />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100');
  });

  it('treats a non-positive limit as unmetered (usage only, no percentage)', () => {
    render(<QuotaMeter label="Aggregate TPS" used={12} limit={0} unit="TPS" />);
    const bar = screen.getByRole('progressbar');
    expect(bar).not.toHaveAttribute('aria-valuenow');
    expect(screen.getByText('12 TPS')).toBeInTheDocument();
  });
});
