import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { useActiveWorkspace, useSessionUser } from '../../../shared/api/useActiveWorkspace';
import { Button, NotFoundPage, PageHeader } from '../../../shared/ui';
import { activityQueryOptions, membersQueryOptions } from '../api';
import { ActivityList } from '../components/ActivityList';
import { DangerZone } from '../components/DangerZone';
import { InviteMemberDialog } from '../components/InviteMemberDialog';
import { MembersTable } from '../components/MembersTable';

/**
 * Workspace settings (frontend-architecture §9.3). Composes MembersTable (with
 * the INV-TEN-3 sole-admin disabled states), ActivityList (audit log), and the
 * DangerZone. Mounted under RequireAdmin, so the audit/members admin APIs are
 * always authorized here.
 */
export function SettingsPage() {
  const ws = useActiveWorkspace();
  const user = useSessionUser();
  const [inviteOpen, setInviteOpen] = useState(false);

  const members = useQuery({ ...membersQueryOptions(ws?.workspaceId ?? ''), enabled: Boolean(ws) });
  const activity = useQuery({
    ...activityQueryOptions(ws?.workspaceId ?? ''),
    enabled: Boolean(ws),
  });

  if (!ws || !user) return <NotFoundPage />;

  return (
    <div className="mx-auto max-w-3xl space-y-10">
      <PageHeader
        title="Workspace settings"
        description={ws.name}
        actions={<Button onClick={() => setInviteOpen(true)}>Invite member</Button>}
      />

      <section aria-labelledby="members-heading">
        <h2 id="members-heading" className="mb-3 text-sm font-semibold uppercase tracking-wide text-text-muted">
          Members
        </h2>
        <MembersTable
          workspaceId={ws.workspaceId}
          members={members.data ?? []}
          isLoading={members.isPending}
          error={members.error}
          currentUserId={user.user_id}
        />
      </section>

      <section aria-labelledby="activity-heading">
        <h2 id="activity-heading" className="mb-3 text-sm font-semibold uppercase tracking-wide text-text-muted">
          Activity
        </h2>
        <ActivityList
          entries={activity.data ?? []}
          isLoading={activity.isPending}
          error={activity.error}
        />
      </section>

      <DangerZone
        workspaceId={ws.workspaceId}
        workspaceName={ws.name}
        workspaceSlug={ws.slug}
      />

      <InviteMemberDialog
        open={inviteOpen}
        onOpenChange={setInviteOpen}
        workspaceId={ws.workspaceId}
      />
    </div>
  );
}
