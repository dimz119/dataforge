import { useState } from 'react';

import type { Membership, RoleEnum } from '../../../shared/api/types';
import {
  Button,
  ConfirmDialog,
  DataTable,
  ErrorState,
  type Column,
  useToast,
} from '../../../shared/ui';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import { useRemoveMember, useUpdateMemberRole } from '../api';
import { isSoleAdmin, SOLE_ADMIN_TOOLTIP } from '../membership';

export interface MembersTableProps {
  workspaceId: string;
  members: Membership[];
  isLoading: boolean;
  error: unknown;
  /** The signed-in user's id — removing self triggers a session refresh. */
  currentUserId: string;
}

function RoleChip({ role }: { role: string }) {
  const isAdmin = role === 'admin';
  return (
    <span
      className={
        isAdmin
          ? 'rounded-full bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent'
          : 'rounded-full bg-surface-muted px-2 py-0.5 text-xs font-medium text-text-muted'
      }
    >
      {role}
    </span>
  );
}

/**
 * MembersTable (frontend-architecture §9.3). Role chips, demote/promote, and
 * remove. The last admin's demote+remove are DISABLED with the INV-TEN-3 tooltip,
 * mirroring the API 409.
 */
export function MembersTable({
  workspaceId,
  members,
  isLoading,
  error,
  currentUserId,
}: MembersTableProps) {
  const updateRole = useUpdateMemberRole(workspaceId);
  const removeMember = useRemoveMember(workspaceId);
  const toast = useToast();
  const [pendingRemove, setPendingRemove] = useState<Membership | null>(null);

  if (error) return <ErrorState error={error} />;

  function changeRole(member: Membership, role: RoleEnum) {
    updateRole.mutate(
      { userId: member.user_id, role },
      {
        onSuccess: () => toast.show({ title: 'Role updated', tone: 'success' }),
        onError: (err) => toast.showError(err, 'Could not update role'),
      },
    );
  }

  function confirmRemove() {
    if (!pendingRemove) return;
    const target = pendingRemove;
    removeMember.mutate(
      { userId: target.user_id, isSelf: target.user_id === currentUserId },
      {
        onSuccess: () => {
          toast.show({ title: 'Member removed', tone: 'success' });
          setPendingRemove(null);
        },
        onError: (err) => {
          toast.showError(err, 'Could not remove member');
          setPendingRemove(null);
        },
      },
    );
  }

  const columns: Column<Membership>[] = [
    { id: 'email', header: 'Member', cell: (m) => <span className="font-medium">{m.email}</span> },
    { id: 'role', header: 'Role', cell: (m) => <RoleChip role={m.role} /> },
    {
      id: 'joined',
      header: 'Joined',
      cell: (m) => <span className="text-text-muted">{formatRelativeTime(m.joined_at)}</span>,
    },
    {
      id: 'actions',
      header: <span className="sr-only">Actions</span>,
      align: 'right',
      cell: (m) => {
        const locked = isSoleAdmin(m, members);
        const toggleTo: RoleEnum = m.role === 'admin' ? 'member' : 'admin';
        return (
          <div className="flex justify-end gap-2">
            <span title={locked ? SOLE_ADMIN_TOOLTIP : undefined}>
              <Button
                variant="ghost"
                size="sm"
                disabled={locked}
                aria-disabled={locked || undefined}
                title={locked ? SOLE_ADMIN_TOOLTIP : undefined}
                onClick={() => changeRole(m, toggleTo)}
              >
                {m.role === 'admin' ? 'Demote' : 'Promote'}
              </Button>
            </span>
            <span title={locked ? SOLE_ADMIN_TOOLTIP : undefined}>
              <Button
                variant="ghost"
                size="sm"
                disabled={locked}
                aria-disabled={locked || undefined}
                title={locked ? SOLE_ADMIN_TOOLTIP : undefined}
                onClick={() => setPendingRemove(m)}
              >
                Remove
              </Button>
            </span>
          </div>
        );
      },
    },
  ];

  return (
    <>
      <DataTable
        columns={columns}
        rows={members}
        rowKey={(m) => m.user_id}
        isLoading={isLoading}
        caption="Workspace members"
      />
      <ConfirmDialog
        open={pendingRemove !== null}
        onOpenChange={(o) => !o && setPendingRemove(null)}
        title="Remove member"
        description={
          pendingRemove
            ? `${pendingRemove.email} will lose access to this workspace.`
            : undefined
        }
        confirmLabel="Remove"
        danger
        loading={removeMember.isPending}
        onConfirm={confirmRemove}
      />
    </>
  );
}
