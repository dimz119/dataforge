import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PerTypeCounters } from './PerTypeCounters';
import { SamplingBadge } from './SamplingBadge';
import { TailRow } from './TailRow';

describe('SamplingBadge', () => {
  it('is hidden when sampling is inactive', () => {
    const { container } = render(<SamplingBadge active={false} keepRatio={1} />);
    expect(container).toBeEmptyDOMElement();
  });
  it('shows the 1/k ratio when active', () => {
    render(<SamplingBadge active keepRatio={1 / 5} />);
    expect(screen.getByText(/1\/5/)).toBeInTheDocument();
  });
});

describe('PerTypeCounters', () => {
  it('splits business and CDC types, top-N by count', () => {
    render(
      <PerTypeCounters
        byEventType={{ order_placed: 100, page_view: 50, 'cdc.orders': 20 }}
      />,
    );
    expect(screen.getByText('Business')).toBeInTheDocument();
    expect(screen.getByText('CDC')).toBeInTheDocument();
    expect(screen.getByText('order_placed')).toBeInTheDocument();
    expect(screen.getByText('cdc.orders')).toBeInTheDocument();
  });
  it('renders an empty hint with no counts', () => {
    render(<PerTypeCounters byEventType={{}} />);
    expect(screen.getByText(/No events counted yet/)).toBeInTheDocument();
  });
});

describe('TailRow', () => {
  it('renders the type chip, sequence, and a CDC op chip', () => {
    render(
      <TailRow
        event={{ event_type: 'cdc.orders', op: 'u', sequence_no: 42, occurred_at: '2026-06-14T10:00:00Z' }}
        expanded={false}
        onToggle={() => {}}
      />,
    );
    expect(screen.getByText('cdc.orders')).toBeInTheDocument();
    expect(screen.getByText('u')).toBeInTheDocument();
    expect(screen.getByText('#42')).toBeInTheDocument();
  });

  it.each([
    ['c', 'CDC create', 'text-status-green'],
    ['u', 'CDC update', 'text-status-amber'],
    ['d', 'CDC delete', 'text-status-red'],
    ['r', 'CDC snapshot read', 'text-status-blue'],
  ])('colors the %s op chip distinctly (Phase 8 CDC op chips)', (op, title, hue) => {
    render(
      <TailRow
        event={{ event_type: `cdc.users`, op, sequence_no: 1, occurred_at: '2026-06-14T10:00:00Z' }}
        expanded={false}
        onToggle={() => {}}
      />,
    );
    const chip = screen.getByTestId('cdc-op-chip');
    expect(chip).toHaveAttribute('data-op', op);
    expect(chip).toHaveAttribute('title', title);
    expect(chip.className).toContain(hue);
  });

  it('omits the op chip for a business event (op null)', () => {
    render(
      <TailRow
        event={{ event_type: 'order_placed', op: null, sequence_no: 7, occurred_at: '2026-06-14T10:00:00Z' }}
        expanded={false}
        onToggle={() => {}}
      />,
    );
    expect(screen.queryByTestId('cdc-op-chip')).not.toBeInTheDocument();
  });
});
