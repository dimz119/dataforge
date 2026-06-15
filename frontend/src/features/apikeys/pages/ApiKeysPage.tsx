import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { useActiveWorkspace } from '../../../shared/api/useActiveWorkspace';
import type { ApiKeyCreated } from '../../../shared/api/types';
import { Button, EmptyState, NotFoundPage, PageHeader } from '../../../shared/ui';
import { apiKeysQueryOptions } from '../api';
import { CreateKeyDialog } from '../components/CreateKeyDialog';
import { KeysTable } from '../components/KeysTable';
import { RevealOnceDialog } from '../components/RevealOnceDialog';

/**
 * API keys page (frontend-architecture §9.6). Lists keys (prefix+last4), the
 * create dialog, and the reveal-once dialog. The plaintext flows create →
 * reveal-once only; it never enters the cache or this page's persisted state.
 */
export function ApiKeysPage() {
  const ws = useActiveWorkspace();
  const [createOpen, setCreateOpen] = useState(false);
  // The just-created key (with plaintext) handed to the reveal-once dialog.
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);

  const keys = useQuery({ ...apiKeysQueryOptions(ws?.workspaceId ?? ''), enabled: Boolean(ws) });

  if (!ws) return <NotFoundPage />;

  const rows = keys.data ?? [];

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        title="API keys"
        description="Keys authenticate your external consumers. The plaintext is shown once."
        actions={<Button onClick={() => setCreateOpen(true)}>Create key</Button>}
      />

      {!keys.isPending && rows.length === 0 ? (
        <EmptyState
          title="No API keys yet"
          description="Create a scoped key so your consumers can pull events from a stream."
          action={<Button onClick={() => setCreateOpen(true)}>Create key</Button>}
        />
      ) : (
        <KeysTable
          workspaceId={ws.workspaceId}
          keys={rows}
          isLoading={keys.isPending}
          error={keys.error}
        />
      )}

      <CreateKeyDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        workspaceId={ws.workspaceId}
        isAdmin={ws.isAdmin}
        onCreated={setCreated}
      />
      <RevealOnceDialog created={created} onClose={() => setCreated(null)} />
    </div>
  );
}
