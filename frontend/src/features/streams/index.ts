/**
 * Streams feature public surface (frontend-architecture §9.5). Exports the routes,
 * the normative control-panel state machine (the §9.5 button-enablement matrix), the
 * control-panel components, and the data layer. The stream LIST + monitoring overview
 * are owned by features/monitoring (§9.7).
 */
export { streamsRoutes, buildStreamsRoutes } from './routes';

// The control-panel state machine (the normative §9.5 button-enablement matrix).
export {
  CONTROL_MATRIX,
  controlRow,
  startLabel,
  startHint,
  QUOTA_RESUME_TOOLTIP,
  ACTION_FOR,
  type StreamStatus,
  type StreamAction,
  type ControlState,
  type ControlRow,
} from './controlMatrix';

// Log-scale TPS helpers + plan caps (shared by the slider and the create form).
export { tpsToPosition, positionToTps, clampTps, TPS_MIN, TPS_MAX } from './tpsScale';
export {
  perStreamTpsCap,
  backfillDaysCap,
  SPEED_MULTIPLIER_MIN,
  SPEED_MULTIPLIER_MAX,
} from './planCaps';

// Control-panel components (§9.5).
export { StreamControlPanel, type StreamControlPanelProps } from './components/StreamControlPanel';
export { LifecycleButtons, type LifecycleButtonsProps } from './components/LifecycleButtons';
export { TpsSlider, type TpsSliderProps } from './components/TpsSlider';
export {
  VirtualClockSection,
  type VirtualClockSectionProps,
  type VirtualClockValue,
  type StreamMode,
} from './components/VirtualClockSection';
export { PinSummary, type PinSummaryProps } from './components/PinSummary';
export { StreamDangerZone, type StreamDangerZoneProps } from './components/StreamDangerZone';

// Data layer (§4): single-stream detail, create, lifecycle verbs, live target_tps.
export {
  streamQueryOptions,
  instancesQueryOptions,
  useCreateStream,
  useStreamLifecycle,
  useSetTargetTps,
} from './api';
