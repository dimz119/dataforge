import { useState } from 'react';
import { useNavigate } from 'react-router';

import { Button, Dialog, FormField, Input, useToast } from '../../../shared/ui';
import { useDeleteWorkspace } from '../api';

export interface DangerZoneProps {
  workspaceId: string;
  workspaceName: string;
  workspaceSlug: string;
}

/**
 * DangerZone (frontend-architecture §9.3). Delete workspace behind a
 * typed-confirmation dialog that enumerates the cascade (INV-TEN-6): every API
 * key is revoked, every stream stopped, all instances and data removed. The
 * confirm button stays disabled until the slug is typed exactly.
 */
export function DangerZone({ workspaceId, workspaceName, workspaceSlug }: DangerZoneProps) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const del = useDeleteWorkspace(workspaceId);
  const toast = useToast();
  const navigate = useNavigate();

  function onConfirm() {
    del.mutate(undefined, {
      onSuccess: () => {
        toast.show({ title: 'Workspace deleted', tone: 'success' });
        void navigate('/', { replace: true });
      },
      onError: (err) => {
        toast.showError(err, 'Could not delete workspace');
        setOpen(false);
      },
    });
  }

  return (
    <section className="rounded-lg border border-danger/40 bg-danger/5 p-5">
      <h2 className="text-base font-semibold text-text">Danger zone</h2>
      <p className="mt-1 text-sm text-text-muted">
        Deleting <span className="font-medium text-text">{workspaceName}</span> is permanent and
        cannot be undone.
      </p>
      <Button variant="danger" className="mt-4" onClick={() => setOpen(true)}>
        Delete workspace
      </Button>

      <Dialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o);
          if (!o) setTyped('');
        }}
        title="Delete workspace"
        description="This cascade is irreversible (INV-TEN-6):"
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)} disabled={del.isPending}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={onConfirm}
              loading={del.isPending}
              disabled={typed !== workspaceSlug}
            >
              Delete workspace
            </Button>
          </>
        }
      >
        <ul className="mb-4 list-disc space-y-1 pl-5 text-sm text-text-muted">
          <li>All API keys are revoked immediately.</li>
          <li>All running streams are stopped.</li>
          <li>All scenario instances and their configuration are removed.</li>
          <li>All members lose access.</li>
        </ul>
        <FormField
          label={`Type "${workspaceSlug}" to confirm`}
          hint="This must match the workspace slug exactly."
        >
          {(p) => (
            <Input
              value={typed}
              autoComplete="off"
              onChange={(e) => setTyped(e.target.value)}
              {...p}
            />
          )}
        </FormField>
      </Dialog>
    </section>
  );
}
