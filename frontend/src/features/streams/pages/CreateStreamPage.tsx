import { useQuery } from '@tanstack/react-query';
import { type FormEvent, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import { ApiError } from '../../../shared/api/problem';
import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import {
  Button,
  EmptyState,
  ErrorState,
  FormField,
  Input,
  NotFoundPage,
  PageHeader,
  PageSkeleton,
  useToast,
} from '../../../shared/ui';
import { instancesQueryOptions, useCreateStream } from '../api';
import { VirtualClockSection, type VirtualClockValue } from '../components/VirtualClockSection';
import { clampTps } from '../tpsScale';

const KNOWN_FIELDS = ['name', 'scenario_instance_id', 'seed', 'target_tps'] as const;

/**
 * Create-stream page (frontend-architecture §9.5 CreateStreamPage). Instance picker
 * (pre-selected via `?instance=`), seed (blank → server-generated; INV-G-4 helper),
 * initial target_tps (1..1,000), and the virtual-clock section. The virtual-clock
 * fields exist from Phase 7 but accept ONLY 1× / live until Phase 8 unlocks them.
 */
export function CreateStreamPage() {
  const ws = useActiveWorkspace();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const create = useCreateStream(ws?.workspaceId ?? '');

  const instances = useQuery({
    ...instancesQueryOptions(ws?.workspaceId ?? ''),
    enabled: Boolean(ws),
  });

  const preselect = params.get('instance') ?? '';
  const [instanceId, setInstanceId] = useState(preselect);
  const [name, setName] = useState('');
  const [seed, setSeed] = useState('');
  const [targetTps, setTargetTps] = useState('10');
  // Phase 8: the virtual-clock controls unlock speed multipliers + backfill mode.
  const [virtualClock, setVirtualClock] = useState<VirtualClockValue>({
    speedMultiplier: 1,
    mode: 'live',
    backfillDays: 7,
  });
  const [errors, setErrors] = useState<{
    name?: string;
    scenario_instance_id?: string;
    seed?: string;
    target_tps?: string;
    form?: string;
  }>({});

  const effectiveInstance = useMemo(() => {
    if (instanceId) return instanceId;
    return instances.data?.[0]?.scenario_instance_id ?? '';
  }, [instanceId, instances.data]);

  if (!ws) return <NotFoundPage />;
  if (instances.isPending) return <PageSkeleton />;
  if (instances.error) {
    return <ErrorState error={instances.error} onRetry={() => void instances.refetch()} />;
  }

  const activeWs = ws;
  const basePath = `/w/${activeWs.slug}`;

  if (instances.data.length === 0) {
    return (
      <div className="mx-auto max-w-xl">
        <PageHeader title="Start a stream" />
        <EmptyState
          title="No scenario instances yet"
          description="Configure a scenario instance first — a stream is started from an instance's pinned config."
          action={
            <Button onClick={() => void navigate(`${basePath}/scenarios`)}>
              Browse scenarios
            </Button>
          }
        />
      </div>
    );
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErrors({});
    if (!effectiveInstance) {
      setErrors({ scenario_instance_id: 'Choose a scenario instance.' });
      return;
    }
    const tps = clampTps(Number.parseInt(targetTps, 10) || 0);
    create.mutate(
      {
        workspace_id: activeWs.workspaceId,
        scenario_instance_id: effectiveInstance,
        name,
        // Blank seed → server-generated (INV-STR-5); omit the key when empty.
        ...(seed.trim() !== '' ? { seed: seed.trim() } : {}),
        target_tps: tps,
        // Shard count is pinned at start (immutable, part of the determinism pin,
        // INV-STR-5). The console runs the MVP single-shard layout (N=1); a per-stream
        // shard picker is not exposed yet, so we send the platform default explicitly
        // now that the create contract (P11-01) requires it.
        shard_count: 1,
        // Phase 8: virtual_clock carries the unlocked speed_multiplier (decimal
        // string per the API contract). Backfill mode is realized via the datasets
        // resource (§4.10), not a stream-create field, so it is not sent here.
        ...(virtualClock.speedMultiplier !== 1
          ? { virtual_clock: { speed_multiplier: String(virtualClock.speedMultiplier) } }
          : {}),
      },
      {
        onSuccess: (stream) => {
          toast.show({ title: 'Stream created', tone: 'success' });
          void navigate(`${basePath}/streams/${stream.stream_id}`);
        },
        onError: (err) => {
          if (err instanceof ApiError && err.slug === 'quota-exceeded') {
            setErrors({ form: err.detail ?? err.title });
            return;
          }
          const mapped = mapValidationProblem(err, KNOWN_FIELDS);
          setErrors({
            name: mapped.fields.name,
            scenario_instance_id: mapped.fields.scenario_instance_id,
            seed: mapped.fields.seed,
            target_tps: mapped.fields.target_tps,
            form: mapped.formLevel.length > 0 ? mapped.formLevel.join(' ') : undefined,
          });
        },
      },
    );
  }

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <PageHeader
        title="Start a stream"
        description="A stream copies its instance's pinned config and seed at creation (T1)."
      />
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
        {errors.form && (
          <p role="alert" className="rounded-md bg-danger/10 px-3 py-2 text-sm text-danger">
            {errors.form}
          </p>
        )}

        <FormField label="Scenario instance" error={errors.scenario_instance_id} required>
          {(p) => (
            <select
              id={p.id}
              value={effectiveInstance}
              onChange={(e) => setInstanceId(e.target.value)}
              className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text"
            >
              {instances.data.map((i) => (
                <option key={i.scenario_instance_id} value={i.scenario_instance_id}>
                  {i.name} — {i.scenario_slug}@{i.manifest_version} (rev {i.config_revision})
                </option>
              ))}
            </select>
          )}
        </FormField>

        <FormField label="Name" error={errors.name} required>
          {(p) => (
            <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" {...p} />
          )}
        </FormField>

        <FormField
          label="Seed"
          error={errors.seed}
          hint="Leave blank for a server-generated seed. The same seed + config reproduces the identical stream (INV-G-4)."
        >
          {(p) => (
            <Input
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
              inputMode="numeric"
              autoComplete="off"
              placeholder="auto"
              {...p}
            />
          )}
        </FormField>

        <FormField
          label="Initial target rate (TPS)"
          error={errors.target_tps}
          hint="1–1,000 events/sec. Adjustable live once running."
          required
        >
          {(p) => (
            <Input
              type="number"
              min={1}
              max={1000}
              value={targetTps}
              onChange={(e) => setTargetTps(e.target.value)}
              {...p}
            />
          )}
        </FormField>

        <VirtualClockSection
          value={virtualClock}
          onChange={setVirtualClock}
        />

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={() => void navigate(-1)}>
            Cancel
          </Button>
          <Button type="submit" loading={create.isPending} disabled={name.trim() === ''}>
            Create stream
          </Button>
        </div>
      </form>
    </div>
  );
}
