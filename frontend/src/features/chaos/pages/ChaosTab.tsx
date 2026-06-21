import { useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { NotFoundPage } from '../../../shared/ui';
import { useDriftEligibility } from '../api';
import { ChaosPanel } from '../components/ChaosPanel';

/**
 * The `chaos` tab content (frontend-architecture §9.5), nested under the stream-detail
 * layout. Resolves the active workspace + `:streamId` then mounts the ChaosPanel. Phase 10
 * wires the schema-drift menu: `hasNextSchemaVersion` (CH-V07 eligibility) gates the
 * `schema_drift` card, and `driftMenu` drives the DriftModeNote (injectable fields or the
 * "no next version" explanation naming the highest registered version per subject).
 */
export function ChaosTab() {
  const ws = useActiveWorkspace();
  const { streamId = '' } = useParams();
  const drift = useDriftEligibility(ws?.workspaceId ?? '', streamId);
  if (!ws || streamId === '') return <NotFoundPage />;
  return (
    <ChaosPanel
      workspaceId={ws.workspaceId}
      streamId={streamId}
      hasNextSchemaVersion={drift.eligible}
      driftMenu={drift.subjects}
    />
  );
}
