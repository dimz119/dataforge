export { scenariosRoutes } from './routes';
export { ScenarioCard, type ScenarioCardProps } from './components/ScenarioCard';
export {
  CreateInstanceDialog,
  type CreateInstanceDialogProps,
} from './components/CreateInstanceDialog';
export {
  scenariosQueryOptions,
  scenarioQueryOptions,
  instancesQueryOptions,
  instanceQueryOptions,
  instanceConfigQueryOptions,
  manifestQueryOptions,
  useCreateInstance,
  useSaveInstanceConfig,
  // Schema registry (Phase 10, §9.4).
  subjectsQueryOptions,
  subjectQueryOptions,
  subjectVersionsQueryOptions,
  subjectVersionQueryOptions,
  schemaDiffQueryOptions,
} from './api';
export { SchemaDiff, type SchemaDiffProps } from './components/SchemaDiff';
export {
  SchemaVersionViewer,
  type SchemaVersionViewerProps,
} from './components/SchemaVersionViewer';

// Overlay shape + manifest readers + the OverlayErrorMap (§9.4).
export {
  readTransitionOverrides,
  readCatalogBounds,
  readCdcEntities,
  readIntensityDefaults,
  CATALOG_SUM_CAP,
  INTENSITY_MAX,
  type Overlay,
  type TransitionOverride,
  type CatalogBound,
  type DwellSpec,
} from './overlay';
export {
  buildOverlayErrorMap,
  locateOverlayError,
  formLevelOverlayErrors,
  type OverlayError,
  type OverlayErrorMap,
} from './overlayErrors';
export { CHAOS_MODES, CHAOS_RATE_MAX, type ChaosMode } from './components/config/ChaosDefaultsSection';
