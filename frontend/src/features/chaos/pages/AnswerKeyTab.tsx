import { useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { NotFoundPage } from '../../../shared/ui';
import { AnswerKeyPanel } from '../components/AnswerKeyPanel';

/**
 * The `answer-key` tab content (frontend-architecture §9.5; ADR-0017), nested under
 * the stream-detail layout. Gated on admin/answer_key:read: the session membership
 * carries the role, so `isAdmin` is the read gate here (the scope is an API-key grant,
 * not a console-session grant); the panel renders the requires-scope state otherwise.
 */
export function AnswerKeyTab() {
  const ws = useActiveWorkspace();
  const { streamId = '' } = useParams();
  if (!ws || streamId === '') return <NotFoundPage />;
  return <AnswerKeyPanel workspaceId={ws.workspaceId} streamId={streamId} canRead={ws.isAdmin} />;
}
