import { fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import { TpsSlider } from './TpsSlider';

const mutate = vi.fn();
vi.mock('../api', () => ({
  useSetTargetTps: () => ({ mutate }),
}));

describe('TpsSlider (debounced optimistic PATCH, §9.5)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mutate.mockClear();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function setup() {
    return renderWithProviders(
      <TpsSlider workspaceId="ws-1" streamId="s-1" value={10} debounceMs={400} />,
    );
  }

  it('does NOT fire the PATCH immediately on slider commit — it debounces 400 ms', () => {
    setup();
    const slider = screen.getByRole('slider', { name: /target events per second/i });
    slider.focus();
    // Arrow keys in Radix fire onValueChange + onValueCommit synchronously.
    fireEvent.keyDown(slider, { key: 'ArrowRight' });

    // Immediately after commit: no network call yet.
    expect(mutate).not.toHaveBeenCalled();

    // Before the debounce window elapses: still nothing.
    vi.advanceTimersByTime(399);
    expect(mutate).not.toHaveBeenCalled();

    // After the window: exactly one PATCH.
    vi.advanceTimersByTime(1);
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it('collapses a rapid drag burst into a single trailing PATCH', () => {
    setup();
    const slider = screen.getByRole('slider', { name: /target events per second/i });
    slider.focus();
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    vi.advanceTimersByTime(100);
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    vi.advanceTimersByTime(100);
    fireEvent.keyDown(slider, { key: 'ArrowRight' });

    expect(mutate).not.toHaveBeenCalled();
    vi.advanceTimersByTime(400);
    expect(mutate).toHaveBeenCalledTimes(1);
  });
});
