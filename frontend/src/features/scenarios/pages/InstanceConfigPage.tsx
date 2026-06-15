import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useParams } from 'react-router';

import { ApiError } from '../../../shared/api/problem';
import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import {
  Button,
  ErrorState,
  NotFoundPage,
  PageHeader,
  PageSkeleton,
  useToast,
} from '../../../shared/ui';
import {
  instanceConfigQueryOptions,
  instanceQueryOptions,
  manifestQueryOptions,
  useSaveInstanceConfig,
} from '../api';
import {
  readCatalogBounds,
  readCdcEntities,
  readIntensityDefaults,
  readTransitionOverrides,
  type DiurnalBucket,
  type DwellSpec,
  type Overlay,
  type WeeklyCurve,
} from '../overlay';
import { buildOverlayErrorMap, formLevelOverlayErrors, type OverlayErrorMap } from '../overlayErrors';
import { CatalogSizeInputs } from '../components/config/CatalogSizeInputs';
import { CdcToggles } from '../components/config/CdcToggles';
import {
  ChaosDefaultsSection,
  type ChaosMode,
  type ChaosModeDefault,
} from '../components/config/ChaosDefaultsSection';
import { DwellEditors } from '../components/config/DwellEditors';
import { IntensityCurveEditor } from '../components/config/IntensityCurveEditor';
import { ProbabilitySliders } from '../components/config/ProbabilitySliders';

/** Strip empty sub-objects so the saved overlay is minimal (full-replacement PUT). */
function compactOverlay(o: Overlay): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (o.probabilities && Object.keys(o.probabilities).length > 0) out.probabilities = o.probabilities;
  if (o.dwell && Object.keys(o.dwell).length > 0) out.dwell = o.dwell;
  if (o.catalog_sizes && Object.keys(o.catalog_sizes).length > 0) out.catalog_sizes = o.catalog_sizes;
  if (o.intensity && (o.intensity.diurnal?.length || Object.keys(o.intensity.weekly ?? {}).length)) {
    out.intensity = o.intensity;
  }
  if (o.cdc_entities && o.cdc_entities.length > 0) out.cdc_entities = o.cdc_entities;
  if (o.chaos && Object.keys(o.chaos).length > 0) out.chaos = o.chaos;
  if (o.simulated_timezone) out.simulated_timezone = o.simulated_timezone;
  return out;
}

/**
 * Instance configuration page (frontend-architecture §9.4 InstanceConfigPage). The
 * overlay editor: probability sliders (clamped to override bounds), dwell editors,
 * catalog-size inputs, intensity-curve editor, CDC toggles, and the chaos-DEFAULTS
 * section (instance defaults; the live chaos panel is Phase 9). MAN-V* errors from a
 * 422 are JSON-Pointer-mapped to the exact control via the OverlayErrorMap. The
 * sticky footer shows config_revision + the permanent PIN-2/PIN-4 banner.
 */
export function InstanceConfigPage() {
  const ws = useActiveWorkspace();
  const { instanceId = '' } = useParams();
  const toast = useToast();

  const instance = useQuery({
    ...instanceQueryOptions(ws?.workspaceId ?? '', instanceId),
    enabled: Boolean(ws) && instanceId !== '',
  });
  const config = useQuery({
    ...instanceConfigQueryOptions(ws?.workspaceId ?? '', instanceId),
    enabled: Boolean(ws) && instanceId !== '',
  });
  const manifest = useQuery({
    ...manifestQueryOptions(
      ws?.workspaceId ?? '',
      instance.data?.scenario_slug ?? '',
      instance.data?.manifest_version ?? '',
    ),
    enabled: Boolean(ws) && instance.data != null,
  });

  const save = useSaveInstanceConfig(ws?.workspaceId ?? '', instanceId);
  const [overlay, setOverlay] = useState<Overlay | null>(null);
  const [errorMap, setErrorMap] = useState<OverlayErrorMap>({});

  // Manifest-derived bounds (memoized; the document is immutable).
  const document = useMemo(() => manifest.data?.document ?? {}, [manifest.data]);
  const overrides = useMemo(() => readTransitionOverrides(document), [document]);
  const catalogBounds = useMemo(() => readCatalogBounds(document), [document]);
  const cdcEntities = useMemo(() => readCdcEntities(document), [document]);
  const intensityDefaults = useMemo(() => readIntensityDefaults(document), [document]);

  // The working overlay = local edits, or the loaded configuration on first render.
  const working: Overlay = overlay ?? (config.data?.configuration as Overlay | undefined) ?? {};

  if (!ws) return <NotFoundPage />;
  if (instance.isPending || config.isPending) return <PageSkeleton />;
  if (instance.error) {
    return <ErrorState error={instance.error} onRetry={() => void instance.refetch()} />;
  }
  if (config.error) {
    return <ErrorState error={config.error} onRetry={() => void config.refetch()} />;
  }
  if (manifest.isPending) return <PageSkeleton />;
  if (manifest.error) {
    return <ErrorState error={manifest.error} onRetry={() => void manifest.refetch()} />;
  }

  function patch(next: Partial<Overlay>) {
    setOverlay({ ...working, ...next });
  }

  function onSave() {
    setErrorMap({});
    save.mutate(compactOverlay(working), {
      onSuccess: (result) => {
        toast.show({
          title: `Saved — config revision ${String(result.config_revision)}`,
          tone: 'success',
        });
        setOverlay(null); // re-sync from the cache (now the saved revision)
      },
      onError: (err) => {
        if (err instanceof ApiError && err.slug === 'manifest-validation-failed') {
          setErrorMap(buildOverlayErrorMap(err));
          return;
        }
        toast.showError(err, 'Could not save configuration');
      },
    });
  }

  const formErrors = formLevelOverlayErrors(errorMap);
  const cdcEnabled = new Set(working.cdc_entities ?? []);
  const diurnal: DiurnalBucket[] = working.intensity?.diurnal ?? intensityDefaults.diurnal;
  const weekly: WeeklyCurve = working.intensity?.weekly ?? intensityDefaults.weekly;

  function setProbability(key: string, value: number) {
    patch({ probabilities: { ...working.probabilities, [key]: value } });
  }
  function setDwell(key: string, spec: DwellSpec) {
    patch({ dwell: { ...working.dwell, [key]: spec } });
  }
  function setCatalog(entity: string, value: number) {
    patch({ catalog_sizes: { ...working.catalog_sizes, [entity]: value } });
  }
  function toggleCdc(entity: string, on: boolean) {
    const next = new Set(cdcEnabled);
    if (on) next.add(entity);
    else next.delete(entity);
    patch({ cdc_entities: [...next] });
  }
  function setDiurnal(index: number, multiplier: number) {
    const next = diurnal.map((b, i) => (i === index ? { ...b, multiplier } : b));
    patch({ intensity: { diurnal: next, weekly } });
  }
  function setWeekly(day: string, multiplier: number) {
    patch({ intensity: { diurnal, weekly: { ...weekly, [day]: multiplier } } });
  }
  function setChaos(mode: ChaosMode, next: ChaosModeDefault) {
    patch({ chaos: { ...working.chaos, [mode]: next } });
  }

  const sections: { id: string; title: string; description: string; body: React.ReactNode }[] = [
    {
      id: 'probabilities',
      title: 'Transition probabilities',
      description: 'Clamped to each transition’s override bounds; the manifest default is marked.',
      body: (
        <ProbabilitySliders
          overrides={overrides}
          values={working.probabilities ?? {}}
          onChange={setProbability}
          errors={errorMap}
        />
      ),
    },
    {
      id: 'dwell',
      title: 'Dwell distributions',
      description: 'Tune parameters; the distribution family is fixed by the manifest.',
      body: (
        <DwellEditors overrides={overrides} values={working.dwell ?? {}} onChange={setDwell} />
      ),
    },
    {
      id: 'catalogs',
      title: 'Catalog sizes',
      description: 'Per-entity, clamped to manifest bounds; Σ capped at 250,000 (B-08).',
      body: (
        <CatalogSizeInputs
          bounds={catalogBounds}
          values={working.catalog_sizes ?? {}}
          onChange={setCatalog}
          errors={errorMap}
        />
      ),
    },
    {
      id: 'intensity',
      title: 'Intensity curves',
      description: 'Diurnal + weekly multipliers; renormalized to mean 1.0 (PRD §4.3).',
      body: (
        <IntensityCurveEditor
          diurnal={diurnal}
          weekly={weekly}
          onDiurnalChange={setDiurnal}
          onWeeklyChange={setWeekly}
          errors={errorMap}
        />
      ),
    },
    {
      id: 'cdc',
      title: 'CDC entities',
      description: 'Toggle change-data-capture per manifest-declared entity (R-CDC-M1).',
      body: (
        <CdcToggles entities={cdcEntities} enabled={cdcEnabled} onToggle={toggleCdc} errors={errorMap} />
      ),
    },
    {
      id: 'chaos',
      title: 'Chaos defaults',
      description: 'Instance defaults inherited by new streams (the live panel is a later phase).',
      body: (
        <ChaosDefaultsSection values={working.chaos ?? {}} onChange={setChaos} errors={errorMap} />
      ),
    },
  ];

  return (
    <div className="mx-auto max-w-3xl pb-28">
      <PageHeader
        title={instance.data.name}
        description={`${instance.data.scenario_slug}@${instance.data.manifest_version}`}
      />

      {formErrors.length > 0 && (
        <ul role="alert" className="mb-6 space-y-1 rounded-md bg-danger/10 px-3 py-2 text-sm text-danger">
          {formErrors.map((e, i) => (
            <li key={i}>{e.message}</li>
          ))}
        </ul>
      )}

      <div className="space-y-8">
        {sections.map((s) => (
          <section key={s.id} aria-labelledby={`${s.id}-heading`}>
            <h2 id={`${s.id}-heading`} className="text-base font-semibold text-text">
              {s.title}
            </h2>
            <p className="mb-3 text-sm text-text-muted">{s.description}</p>
            {s.body}
          </section>
        ))}
      </div>

      {/* Sticky footer: config_revision + the permanent PIN-2/PIN-4 banner. */}
      <footer className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface/95 backdrop-blur">
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-4 px-4 py-3">
          <div>
            <p className="text-sm font-medium text-text">
              Config revision {config.data.config_revision}
            </p>
            <p className="text-xs text-text-muted">
              Changes apply to streams started after saving (PIN-2/PIN-4).
            </p>
          </div>
          <Button onClick={onSave} loading={save.isPending} disabled={overlay === null}>
            Save configuration
          </Button>
        </div>
      </footer>
    </div>
  );
}
