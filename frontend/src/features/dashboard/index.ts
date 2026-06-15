/**
 * Dashboard feature public surface (frontend-architecture §9.2): the route, the data
 * layer (workspace summary + stream stats), and the cards/panel components.
 */
export { dashboardRoutes } from './routes';
export {
  workspaceDetailQueryOptions,
  streamsQueryOptions,
  streamStatsQueryOptions,
} from './api';
export { WorkspaceSummaryCard } from './components/WorkspaceSummaryCard';
export { StreamStatsCard } from './components/StreamStatsCard';
export { GettingStartedPanel } from './components/GettingStartedPanel';
