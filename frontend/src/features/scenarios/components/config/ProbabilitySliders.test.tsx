import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../../../shared/api/problem';
import type { TransitionOverride } from '../../overlay';
import { buildOverlayErrorMap } from '../../overlayErrors';
import { ProbabilitySliders } from './ProbabilitySliders';

const OVERRIDES: TransitionOverride[] = [
  {
    key: 'shopping_session.checkout_started.order_placed',
    machine: 'shopping_session',
    state: 'checkout_started',
    to: 'order_placed',
    default: 0.7,
    allowed: true,
    min: 0.1,
    max: 0.95,
  },
  {
    key: 'shopping_session.checkout_started.abandoned',
    machine: 'shopping_session',
    state: 'checkout_started',
    to: 'abandoned',
    default: 0.3,
    allowed: false, // non-overridable → read-only
    min: 0,
    max: 1,
  },
];

/** The MAN-V201 422 (api-spec §2.7.4) the OverlayErrorMap turns into a control map. */
function manV201Map() {
  return buildOverlayErrorMap(
    new ApiError({
      status: 422,
      type: 'https://docs.dataforge.dev/problems/manifest-validation-failed',
      title: 'Manifest validation failed',
      errors: [
        {
          pointer: '/state_machines/shopping_session/states/checkout_started',
          detail: 'probabilities sum to 1.15; must be <= 1.0',
          code: 'MAN-V201',
          bound: 1.0,
          actual: 1.15,
        },
      ],
    }),
  );
}

describe('ProbabilitySliders + OverlayErrorMap (§9.4)', () => {
  it('renders one slider per transition, marks the manifest default, locks non-overridable', () => {
    render(
      <ProbabilitySliders overrides={OVERRIDES} values={{}} onChange={vi.fn()} errors={{}} />,
    );
    expect(screen.getByText(/default 0.70/)).toBeInTheDocument();
    // The non-overridable transition's slider is disabled.
    const locked = screen.getByRole('slider', {
      name: /checkout_started → abandoned probability/i,
    });
    expect(locked).toHaveAttribute('aria-disabled', 'true');
  });

  it('clamps the slider range to [override.min, override.max]', () => {
    render(
      <ProbabilitySliders overrides={OVERRIDES} values={{}} onChange={vi.fn()} errors={{}} />,
    );
    const slider = screen.getByRole('slider', {
      name: /checkout_started → order_placed probability/i,
    });
    expect(slider).toHaveAttribute('aria-valuemin', '0.1');
    expect(slider).toHaveAttribute('aria-valuemax', '0.95');
  });

  it('surfaces a MAN-V201 sum error on the EXACT offending state group via OverlayErrorMap', () => {
    render(
      <ProbabilitySliders
        overrides={OVERRIDES}
        values={{ 'shopping_session.checkout_started.order_placed': 0.85 }}
        onChange={vi.fn()}
        errors={manV201Map()}
      />,
    );
    // The group fieldset (machine · state) carries the alert with the exact message.
    const group = screen
      .getByText('checkout_started', { exact: false })
      .closest('fieldset') as HTMLElement;
    const alert = within(group).getByRole('alert');
    expect(alert).toHaveTextContent('MAN-V201');
    expect(alert).toHaveTextContent('1.15');
  });
});
