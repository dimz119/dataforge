import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';

import { ApiError } from '../../../shared/api/problem';
import { Button, ErrorState, PageSkeleton, useToast } from '../../../shared/ui';
import { chaosQueryOptions, useUpdateChaos } from '../api';
import {
  CHAOS_MODES,
  defaultMode,
  MODE_META,
  type ChaosMode,
  type ChaosModeConfig,
  type ChaosPolicyDocument,
} from '../types';
import { ChaosModeCard } from './ChaosModeCard';
import { OnStopPolicySelect } from './OnStopPolicySelect';
import { PresetPicker } from './PresetPicker';

export interface ChaosPanelProps {
  workspaceId: string;
  streamId: string;
  /** True when the pinned manifest has a registered next schema version (INV-REG-5). */
  hasNextSchemaVersion?: boolean;
}

/** Normalize the loosely-typed `modes` map into the closed seven-mode document. */
function toDocument(modes: Record<string, unknown> | undefined): ChaosPolicyDocument {
  const src = modes ?? {};
  const doc = {
    on_stop_policy: src.on_stop_policy === 'flush' ? 'flush' : 'discard',
  } as ChaosPolicyDocument;
  for (const mode of CHAOS_MODES) {
    const entry = src[mode] as Partial<ChaosModeConfig> | undefined;
    doc[mode] = {
      enabled: Boolean(entry?.enabled),
      rate: typeof entry?.rate === 'number' ? entry.rate : defaultMode().rate,
      params: entry?.params ?? {},
    };
  }
  return doc;
}

/** Map a chaos `validation-error` (CH-V*) onto the offending mode by JSON Pointer. */
function modeErrors(err: unknown): Partial<Record<ChaosMode, string>> {
  const out: Partial<Record<ChaosMode, string>> = {};
  if (!(err instanceof ApiError) || !err.errors) return out;
  for (const fe of err.errors) {
    const seg = fe.pointer.split('/').filter(Boolean);
    const mode = CHAOS_MODES.find((m) => seg.includes(m));
    if (mode) out[mode] = fe.detail;
  }
  return out;
}

/**
 * ChaosPanel (frontend-architecture §9.5) — the `chaos` tab. Renders the 7 mode cards,
 * the PresetPicker, and the OnStopPolicySelect over a live-mutable ChaosPolicy (PIN-3).
 * Edits PATCH optimistically (§4.3) and re-validate (CH-V*); 422s surface on the
 * offending card. `schema_drift` is disabled until a next schema version exists.
 */
export function ChaosPanel({ workspaceId, streamId, hasNextSchemaVersion = false }: ChaosPanelProps) {
  const toast = useToast();
  const query = useQuery(chaosQueryOptions(workspaceId, streamId));
  const update = useUpdateChaos(workspaceId, streamId);
  const [errors, setErrors] = useState<Partial<Record<ChaosMode, string>>>({});

  const server = useMemo(
    () => (query.data ? toDocument(query.data.modes as Record<string, unknown>) : null),
    [query.data],
  );
  const [draft, setDraft] = useState<ChaosPolicyDocument | null>(null);
  useEffect(() => {
    if (server) setDraft(server);
  }, [server]);

  if (query.isPending) return <PageSkeleton />;
  if (query.error) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (!draft) return <PageSkeleton />;

  const commit = (next: ChaosPolicyDocument) => {
    setDraft(next);
    setErrors({});
    update.mutate(next, {
      onError: (err) => {
        const mapped = modeErrors(err);
        setErrors(mapped);
        if (Object.keys(mapped).length === 0) toast.showError(err, 'Could not update chaos policy');
      },
    });
  };

  const setMode = (mode: ChaosMode, config: ChaosModeConfig) =>
    commit({ ...draft, [mode]: config });

  return (
    <div className="space-y-5">
      <section className="space-y-4 rounded-lg border border-border bg-surface p-5">
        <PresetPicker current={draft} onApply={commit} />
        <div className="border-t border-border pt-4">
          <OnStopPolicySelect
            value={draft.on_stop_policy}
            onChange={(p) => commit({ ...draft, on_stop_policy: p })}
          />
        </div>
      </section>

      <div className="grid gap-3 sm:grid-cols-2">
        {CHAOS_MODES.map((mode) => {
          const drift = mode === 'schema_drift' && !hasNextSchemaVersion;
          return (
            <ChaosModeCard
              key={mode}
              mode={mode}
              config={draft[mode]}
              onChange={(c) => setMode(mode, c)}
              disabled={drift}
              disabledNote={
                drift
                  ? `${MODE_META.schema_drift.label} cannot arm until a next schema version is registered (INV-REG-5).`
                  : undefined
              }
              error={errors[mode]}
            />
          );
        })}
      </div>

      <p className="text-xs text-text-muted" aria-live="polite">
        {update.isPending ? 'Saving…' : 'Changes apply within 2 s (PIN-3).'}
        {server && draft !== server && !update.isPending && (
          <Button
            variant="ghost"
            size="sm"
            className="ml-2"
            onClick={() => {
              setDraft(server);
              setErrors({});
            }}
          >
            Revert
          </Button>
        )}
      </p>
    </div>
  );
}
