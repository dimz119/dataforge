import { type FormEvent, useState } from 'react';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import type { RoleEnum } from '../../../shared/api/types';
import { Button, Dialog, FormField, Input, useToast } from '../../../shared/ui';
import { useInviteMember } from '../api';

export interface InviteMemberDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
}

/**
 * Invite-by-email dialog (frontend-architecture §9.3 MembersTable). Email + role
 * (member/admin). `validation-error` pointers map back onto the email field;
 * `conflict` (already a member) surfaces as a toast (§10.1).
 */
export function InviteMemberDialog({ open, onOpenChange, workspaceId }: InviteMemberDialogProps) {
  const invite = useInviteMember(workspaceId);
  const toast = useToast();
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<RoleEnum>('member');
  const [emailError, setEmailError] = useState<string>();

  function reset() {
    setEmail('');
    setRole('member');
    setEmailError(undefined);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setEmailError(undefined);
    invite.mutate(
      { email, role },
      {
        onSuccess: () => {
          toast.show({ title: 'Invitation sent', tone: 'success' });
          reset();
          onOpenChange(false);
        },
        onError: (err) => {
          const mapped = mapValidationProblem(err, ['email']);
          if (mapped.fields.email) setEmailError(mapped.fields.email);
          else toast.showError(err, 'Could not invite member');
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) reset();
        onOpenChange(o);
      }}
      title="Invite member"
      description="They receive an email to join this workspace."
    >
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        <FormField label="Email" error={emailError} required>
          {(p) => (
            <Input
              type="email"
              autoComplete="off"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              {...p}
            />
          )}
        </FormField>
        <FormField label="Role">
          {(p) => (
            <select
              id={p.id}
              value={role}
              onChange={(e) => setRole(e.target.value as RoleEnum)}
              className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text"
            >
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          )}
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" loading={invite.isPending}>
            Send invite
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
