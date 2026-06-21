import { screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import type { AnswerKeyInjection, AnswerKeySummary } from '../../../shared/api/types';
import { AnswerKeyPanel } from './AnswerKeyPanel';

const summary: AnswerKeySummary = {
  stream_id: 's-1',
  window: { from: null, to: null },
  by_mode: { duplicates: 12, late_arriving: 4 },
  total_injections: 16,
  as_of: '2026-06-14T10:00:00Z',
};

const injection: AnswerKeyInjection = {
  injection_id: 'inj-1',
  mode: 'duplicates',
  stream_id: 's-1',
  shard_id: 0,
  event_id: 'abcdef12-0000-0000-0000-000000000000',
  sequence_no: 42,
  occurred_at: '2026-06-14T10:00:01Z',
  canonical_emitted_at: '2026-06-14T10:00:00Z',
  recorded_at: '2026-06-14T10:00:02Z',
};

vi.mock('../api', () => ({
  answerKeySummaryOptions: () => ({
    queryKey: ['ak-summary'],
    queryFn: () => Promise.resolve(summary),
  }),
  answerKeyInjectionsOptions: () => ({
    queryKey: ['ak-injections'],
    initialPageParam: undefined,
    queryFn: () => Promise.resolve({ data: [injection], next_cursor: null }),
    getNextPageParam: () => undefined,
  }),
  flattenInjections: (pages: { data: AnswerKeyInjection[] }[]) => pages.flatMap((p) => p.data),
  downloadAnswerKeyJsonl: vi.fn(),
}));

describe('AnswerKeyPanel (frontend-architecture §9.5; ADR-0017)', () => {
  it('renders the per-mode summary counts and the injection list when permitted', async () => {
    renderWithProviders(<AnswerKeyPanel workspaceId="ws-1" streamId="s-1" canRead />);

    // Summary header total + a per-mode count.
    expect(await screen.findByText('16 total')).toBeInTheDocument();
    expect(await screen.findByText('12')).toBeInTheDocument();

    // The injection row (sequence_no + truncated event id).
    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(await screen.findByText(/abcdef12…/)).toBeInTheDocument();

    // The export control.
    expect(screen.getByRole('button', { name: /Download JSONL/i })).toBeInTheDocument();
  });

  it('shows the requires-scope empty state without admin/answer_key:read', () => {
    renderWithProviders(
      <AnswerKeyPanel workspaceId="ws-1" streamId="s-1" canRead={false} />,
    );
    expect(screen.getByText(/Answer key is restricted/i)).toBeInTheDocument();
    expect(screen.getByText(/answer_key:read/)).toBeInTheDocument();
    // The list/summary must NOT render when gated.
    expect(screen.queryByText('16 total')).not.toBeInTheDocument();
  });
});
