import { useState } from 'react';

import type { ApiKeyListItem } from '../../../shared/api/types';
import {
  Button,
  ConfirmDialog,
  DataTable,
  ErrorState,
  StatusBadge,
  type Column,
  useToast,
} from '../../../shared/ui';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import { useRevokeApiKey } from '../api';

export interface KeysTableProps {
  workspaceId: string;
  keys: ApiKeyListItem[];
  isLoading: boolean;
  error: unknown;
}

/** Masked display: `df_live_a3f8……9c2e` — never the full key (§9.6, INV-TEN-4). */
function maskedKey(item: ApiKeyListItem): string {
  return `${item.prefix}……${item.last4}`;
}

const KEY_STATE_TONE: Record<string, string> = {
  active: 'running',
  revoked: 'failed',
  expired: 'stopped',
};

/**
 * KeysTable (frontend-architecture §9.6). Shows prefix+last4 ONLY — the full key
 * is never present (it lived only in the reveal-once dialog). Scope chips, state,
 * creator, `last_used_at` relative; revoke behind a confirm.
 */
export function KeysTable({ workspaceId, keys, isLoading, error }: KeysTableProps) {
  const revoke = useRevokeApiKey(workspaceId);
  const toast = useToast();
  const [pending, setPending] = useState<ApiKeyListItem | null>(null);

  if (error) return <ErrorState error={error} />;

  function confirmRevoke() {
    if (!pending) return;
    revoke.mutate(pending.api_key_id, {
      onSuccess: () => {
        toast.show({ title: 'Key revoked', tone: 'success' });
        setPending(null);
      },
      onError: (err) => {
        toast.showError(err, 'Could not revoke key');
        setPending(null);
      },
    });
  }

  const columns: Column<ApiKeyListItem>[] = [
    {
      id: 'name',
      header: 'Name',
      cell: (k) => (
        <div className="flex flex-col">
          <span className="font-medium text-text">{k.name}</span>
          <code className="font-mono text-xs text-text-muted">{maskedKey(k)}</code>
        </div>
      ),
    },
    {
      id: 'scopes',
      header: 'Scopes',
      cell: (k) => (
        <div className="flex flex-wrap gap-1">
          {k.scopes.map((s) => (
            <span
              key={s}
              className="rounded bg-surface-muted px-1.5 py-0.5 font-mono text-[10px] text-text-muted"
            >
              {s}
            </span>
          ))}
        </div>
      ),
    },
    {
      id: 'state',
      header: 'State',
      cell: (k) => <StatusBadge status={KEY_STATE_TONE[k.state] ?? k.state} />,
    },
    {
      id: 'last_used',
      header: 'Last used',
      cell: (k) => <span className="text-text-muted">{formatRelativeTime(k.last_used_at)}</span>,
    },
    {
      id: 'actions',
      header: <span className="sr-only">Actions</span>,
      align: 'right',
      cell: (k) =>
        k.state === 'active' ? (
          <Button variant="ghost" size="sm" onClick={() => setPending(k)}>
            Revoke
          </Button>
        ) : null,
    },
  ];

  return (
    <>
      <DataTable
        columns={columns}
        rows={keys}
        rowKey={(k) => k.api_key_id}
        isLoading={isLoading}
        caption="API keys"
      />
      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(o) => !o && setPending(null)}
        title="Revoke API key"
        description={
          pending
            ? `"${pending.name}" stops working immediately. Consumers using it will be rejected.`
            : undefined
        }
        confirmLabel="Revoke"
        danger
        loading={revoke.isPending}
        onConfirm={confirmRevoke}
      />
    </>
  );
}
