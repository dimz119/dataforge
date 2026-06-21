import { useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { NotFoundPage } from '../../../shared/ui';
import { ChaosPanel } from '../components/ChaosPanel';

/**
 * The `chaos` tab content (frontend-architecture §9.5), nested under the stream-detail
 * layout. Resolves the active workspace + `:streamId` then mounts the ChaosPanel.
 * `hasNextSchemaVersion` is false in the MVP (no registry surface yet, Phase 10), so
 * `schema_drift` renders disabled with its INV-REG-5 note.
 */
export function ChaosTab() {
  const ws = useActiveWorkspace();
  const { streamId = '' } = useParams();
  if (!ws || streamId === '') return <NotFoundPage />;
  return <ChaosPanel workspaceId={ws.workspaceId} streamId={streamId} />;
}
