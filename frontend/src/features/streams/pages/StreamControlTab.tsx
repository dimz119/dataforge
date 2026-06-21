import { useQuery } from '@tanstack/react-query';
import { useParams } from 'react-router';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import { ErrorState, NotFoundPage, PageSkeleton } from '../../../shared/ui';
import { streamQueryOptions } from '../api';
import { StreamControlPanel } from '../components/StreamControlPanel';

/**
 * The `control` tab content (frontend-architecture §9.5), the index child of the
 * stream-detail layout. Reads the same status-keyed stream query (cache shared by
 * key with the layout) and renders the StreamControlPanel.
 */
export function StreamControlTab() {
  const ws = useActiveWorkspace();
  const { streamId = '' } = useParams();
  const stream = useQuery({
    ...streamQueryOptions(ws?.workspaceId ?? '', streamId),
    enabled: Boolean(ws) && streamId !== '',
  });
  const live = useQuery({
    ...streamQueryOptions(ws?.workspaceId ?? '', streamId, stream.data?.status),
    enabled: Boolean(ws) && streamId !== '' && stream.data != null,
  });

  if (!ws) return <NotFoundPage />;
  if (stream.isPending) return <PageSkeleton />;
  if (stream.error) return <ErrorState error={stream.error} onRetry={() => void stream.refetch()} />;

  const data = live.data ?? stream.data;
  return <StreamControlPanel workspaceId={ws.workspaceId} stream={data} />;
}
