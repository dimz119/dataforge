import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { tokenManager } from '../../../shared/api/client';
import { ToastProvider } from '../../../shared/ui';
import { makeFakeFactory } from '../../../shared/ws';
import type { ReadyFrame } from '../../../shared/ws';
import { LiveTail } from './LiveTail';
import { EntityFilter } from './EntityFilter';

const ready: ReadyFrame = {
  type: 'ready',
  protocol: 'dataforge.events.v1',
  stream_id: 's1',
  position: { cursor: 'c0' },
  filters: {},
};

describe('EntityFilter (Phase 8 per-entity CDC filter, R-CDC-7)', () => {
  it('requires both fields before it can apply (both or neither)', async () => {
    const onChange = vi.fn();
    render(<EntityFilter value={null} onChange={onChange} />);
    const apply = screen.getByRole('button', { name: /filter entity/i });
    expect(apply).toBeDisabled();

    await userEvent.type(screen.getByLabelText('Entity type'), 'users');
    expect(apply).toBeDisabled(); // entity_key still empty → not applicable
    await userEvent.type(screen.getByLabelText('Entity key'), 'usr_a');
    expect(apply).toBeEnabled();
    await userEvent.click(apply);
    expect(onChange).toHaveBeenCalledWith({ entityType: 'users', entityKey: 'usr_a' });
  });

  it('clears the filter to null', async () => {
    const onChange = vi.fn();
    render(<EntityFilter value={{ entityType: 'users', entityKey: 'usr_a' }} onChange={onChange} />);
    await userEvent.click(screen.getByLabelText('Clear entity filter'));
    expect(onChange).toHaveBeenCalledWith(null);
  });
});

describe('LiveTail wires the entity filter into the WS auth frame', () => {
  beforeEach(() => {
    vi.spyOn(tokenManager, 'getValidAccessToken').mockResolvedValue('jwt-test');
    vi.spyOn(tokenManager, 'refresh').mockResolvedValue('jwt-test');
  });
  afterEach(() => vi.restoreAllMocks());

  it('applying entity_type+entity_key recreates the socket with both in the auth frame', async () => {
    const fake = makeFakeFactory();
    render(
      <ToastProvider>
        <LiveTail streamId="s1" streamStatus="running" transportFactory={fake.factory} />
      </ToastProvider>,
    );
    await waitFor(() => expect(fake.sockets.length).toBe(1));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
    });
    // The first socket's auth frame carries no entity filter.
    expect(fake.sockets[0].authFrame()).not.toHaveProperty('entity_type');

    await userEvent.type(screen.getByLabelText('Entity type'), 'users');
    await userEvent.type(screen.getByLabelText('Entity key'), 'usr_a');
    await userEvent.click(screen.getByRole('button', { name: /filter entity/i }));

    // A filter change recreates the socket (WS-5); the new auth frame carries the pair.
    await waitFor(() => expect(fake.sockets.length).toBe(2));
    act(() => {
      fake.last().open();
      fake.last().emit(ready);
    });
    const auth = fake.last().authFrame();
    expect(auth).toMatchObject({ entity_type: 'users', entity_key: 'usr_a' });
  });
});
