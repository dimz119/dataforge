import { type FormEvent, useState } from 'react';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import type { ApiKeyCreated, ScopesEnum } from '../../../shared/api/types';
import { Button, Dialog, FormField, Input, useToast } from '../../../shared/ui';
import { useCreateApiKey } from '../api';
import { selectableScopes } from '../scopes';

export interface CreateKeyDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  isAdmin: boolean;
  /** Hand the 201 (with the plaintext) to the reveal-once dialog. */
  onCreated: (created: ApiKeyCreated) => void;
}

/**
 * CreateKeyDialog (frontend-architecture §9.6). Name, scope checkboxes
 * (`answer_key:read` only for admins), optional `expires_at`. On success the
 * plaintext-bearing result is handed straight to the reveal-once dialog — this
 * component never reads or stores the secret.
 */
export function CreateKeyDialog({
  open,
  onOpenChange,
  workspaceId,
  isAdmin,
  onCreated,
}: CreateKeyDialogProps) {
  const create = useCreateApiKey(workspaceId);
  const toast = useToast();
  const scopeOptions = selectableScopes(isAdmin);

  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<Set<ScopesEnum>>(new Set(['events:read']));
  const [expiresAt, setExpiresAt] = useState('');
  const [errors, setErrors] = useState<{ name?: string; scopes?: string; form?: string }>({});

  function reset() {
    setName('');
    setScopes(new Set(['events:read']));
    setExpiresAt('');
    setErrors({});
  }

  function toggleScope(scope: ScopesEnum) {
    setScopes((prev) => {
      const next = new Set(prev);
      if (next.has(scope)) next.delete(scope);
      else next.add(scope);
      return next;
    });
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErrors({});
    if (scopes.size === 0) {
      setErrors({ scopes: 'Select at least one scope.' });
      return;
    }
    create.mutate(
      {
        name,
        scopes: Array.from(scopes),
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      },
      {
        onSuccess: (created) => {
          reset();
          onOpenChange(false);
          onCreated(created);
        },
        onError: (err) => {
          const mapped = mapValidationProblem(err, ['name', 'scopes', 'expires_at']);
          if (mapped.fields.name || mapped.fields.scopes || mapped.formLevel.length > 0) {
            setErrors({
              name: mapped.fields.name,
              scopes: mapped.fields.scopes,
              form: mapped.formLevel.length > 0 ? mapped.formLevel.join(' ') : undefined,
            });
          } else {
            toast.showError(err, 'Could not create key');
          }
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
      title="Create API key"
      description="Scope the key to exactly what your consumer needs."
    >
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        {errors.form && (
          <p role="alert" className="rounded-md bg-danger/10 px-3 py-2 text-sm text-danger">
            {errors.form}
          </p>
        )}
        <FormField label="Name" error={errors.name} required>
          {(p) => (
            <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" {...p} />
          )}
        </FormField>

        <fieldset className="flex flex-col gap-2">
          <legend className="text-sm font-medium text-text">Scopes</legend>
          {scopeOptions.map((s) => (
            <label key={s.value} className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={scopes.has(s.value)}
                onChange={() => toggleScope(s.value)}
                aria-label={s.label}
                className="mt-0.5"
              />
              <span className="font-mono text-text">{s.label}</span>
              <span className="block text-xs text-text-muted">{s.description}</span>
            </label>
          ))}
          {errors.scopes && (
            <p role="alert" className="text-xs text-danger">
              {errors.scopes}
            </p>
          )}
        </fieldset>

        <FormField label="Expires at" hint="Optional. Leave blank for a non-expiring key.">
          {(p) => (
            <Input
              type="date"
              value={expiresAt}
              onChange={(e) => setExpiresAt(e.target.value)}
              {...p}
            />
          )}
        </FormField>

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" loading={create.isPending} disabled={name.trim() === ''}>
            Create key
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
