import { useMemo, useState } from 'react';

import { ApiError } from '../../../shared/api/problem';
import { Button, FormField, Input, useToast } from '../../../shared/ui';
import type { SubjectSummary } from '../../../shared/api/types';
import { useScheduleSchemaUpgrade } from '../api';

export interface ScheduleUpgradeFormProps {
  workspaceId: string;
  streamId: string;
  /** All registry subjects (for the picker eligibility). */
  subjects: SubjectSummary[];
  /** The stream's current effective version per subject (§10.2). */
  effective: Record<string, number>;
}

/** A subject is a CDC feed when its name carries the `.cdc.` segment (REG-U006: no upgrade). */
function isCdc(subject: string): boolean {
  return subject.includes('.cdc.');
}

/** Eligible = business subject whose latest registered version exceeds the effective. */
interface Eligible {
  subject: string;
  effective: number;
  latest: number;
}

/**
 * Schedule-a-mid-stream-upgrade form (frontend-architecture §9.5). The subject picker is
 * limited to BUSINESS subjects (CDC excluded, REG-U006) that have a registered version
 * higher than the stream's current effective version — there is nothing to upgrade to
 * otherwise. `at` is SIMULATED time (`datetime-local` interpreted in the occurred_at
 * domain, RFC 3339 to the wire); leaving it blank schedules the next tick. REG-U001..U007
 * validation failures arrive as a 409 `conflict` with `errors[]`; each is rendered inline
 * under the form (the runner cannot have validated yet, so this is the only surface).
 */
export function ScheduleUpgradeForm({
  workspaceId,
  streamId,
  subjects,
  effective,
}: ScheduleUpgradeFormProps) {
  const toast = useToast();
  const schedule = useScheduleSchemaUpgrade(workspaceId, streamId);

  const eligible = useMemo<Eligible[]>(() => {
    const out: Eligible[] = [];
    for (const s of subjects) {
      if (isCdc(s.subject) || s.latest_version == null) continue;
      const eff = effective[s.subject] ?? 1;
      if (s.latest_version > eff) {
        out.push({ subject: s.subject, effective: eff, latest: s.latest_version });
      }
    }
    return out.sort((a, b) => a.subject.localeCompare(b.subject));
  }, [subjects, effective]);

  const [subject, setSubject] = useState('');
  const [targetVersion, setTargetVersion] = useState('');
  const [at, setAt] = useState('');
  const [errors, setErrors] = useState<{ code?: string; message: string }[]>([]);

  const picked = eligible.find((e) => e.subject === subject) ?? null;
  // Offer every registered version above the effective as a cutover target.
  const targetOptions = picked
    ? Array.from({ length: picked.latest - picked.effective }, (_, i) => picked.effective + 1 + i)
    : [];

  if (eligible.length === 0) {
    return (
      <p className="rounded-md bg-surface-muted px-3 py-2 text-xs text-text-muted" role="note">
        No upgrade is available: every business subject is already at its highest
        registered version. Register a new version to schedule an evolution.
      </p>
    );
  }

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setErrors([]);
    const target = Number.parseInt(targetVersion, 10);
    if (!subject || !Number.isFinite(target)) return;
    schedule.mutate(
      {
        subject,
        target_version: target,
        // datetime-local has no zone; treat it as UTC simulated time (occurred_at domain).
        at: at ? new Date(at).toISOString() : null,
      },
      {
        onSuccess: () => {
          setSubject('');
          setTargetVersion('');
          setAt('');
          toast.show({ tone: 'success', title: 'Upgrade scheduled' });
        },
        onError: (err) => {
          if (err instanceof ApiError && err.errors && err.errors.length > 0) {
            setErrors(err.errors.map((fe) => ({ code: fe.code, message: fe.detail })));
          } else {
            toast.showError(err, 'Could not schedule the upgrade');
          }
        },
      },
    );
  };

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <FormField label="Subject" hint="Business subjects with a higher registered version.">
          {(p) => (
            <select
              id={p.id}
              value={subject}
              onChange={(e) => {
                setSubject(e.target.value);
                setTargetVersion('');
                setErrors([]);
              }}
              className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text"
            >
              <option value="">Select a subject…</option>
              {eligible.map((e) => (
                <option key={e.subject} value={e.subject}>
                  {e.subject} (v{e.effective} → v{e.latest})
                </option>
              ))}
            </select>
          )}
        </FormField>

        <FormField label="Target version" hint="The registered version to evolve to.">
          {(p) => (
            <select
              id={p.id}
              value={targetVersion}
              onChange={(e) => setTargetVersion(e.target.value)}
              disabled={!picked}
              className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text disabled:opacity-50"
            >
              <option value="">Select a version…</option>
              {targetOptions.map((v) => (
                <option key={v} value={v}>
                  v{v}
                </option>
              ))}
            </select>
          )}
        </FormField>
      </div>

      <FormField
        label="Cutover at (simulated time)"
        hint="When the stream's virtual clock reaches this instant. Leave blank for the next tick."
      >
        {(p) => (
          <Input type="datetime-local" value={at} onChange={(e) => setAt(e.target.value)} {...p} />
        )}
      </FormField>

      {errors.length > 0 && (
        <ul className="space-y-1 rounded-md border border-danger/40 bg-danger/5 p-3" role="alert">
          {errors.map((err, i) => (
            <li key={i} className="text-xs text-danger">
              {err.code && <span className="font-mono font-semibold">{err.code}: </span>}
              {err.message}
            </li>
          ))}
        </ul>
      )}

      <Button type="submit" loading={schedule.isPending} disabled={!subject || !targetVersion}>
        Schedule upgrade
      </Button>
    </form>
  );
}
