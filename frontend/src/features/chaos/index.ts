/** Public surface of the chaos feature (Phase 9). The app router nests the tab routes. */
export { chaosTabRoutes } from './routes';

// Schema-drift menu + eligibility (Phase 10, DR-1..3 / CH-V07).
export {
  useDriftEligibility,
  type DriftEligibility,
  type DriftSubjectMenu,
} from './api';
export { DriftModeNote, type DriftModeNoteProps } from './components/DriftModeNote';
