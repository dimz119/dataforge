import { type FormEvent, useState } from 'react';
import { useNavigate } from 'react-router';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import { Button, FormField, Input, PageHeader, useToast } from '../../../shared/ui';
import { useCreateWorkspace } from '../api';
import { deriveSlug } from '../slug';

/**
 * CreateWorkspaceForm (frontend-architecture §9.3). Name + auto-derived editable
 * slug; the API owns slug uniqueness (`conflict`/`validation-error` mapped back
 * to the fields). On success we route to the new workspace dashboard.
 */
export function CreateWorkspacePage() {
  const create = useCreateWorkspace();
  const navigate = useNavigate();
  const toast = useToast();

  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [slugEdited, setSlugEdited] = useState(false);
  const [errors, setErrors] = useState<{ name?: string; slug?: string; form?: string }>({});

  const effectiveSlug = slugEdited ? slug : deriveSlug(name);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErrors({});
    create.mutate(
      { name, slug: effectiveSlug || null },
      {
        onSuccess: (ws) => {
          toast.show({ title: 'Workspace created', tone: 'success' });
          void navigate(`/w/${ws.slug}/dashboard`);
        },
        onError: (err) => {
          const mapped = mapValidationProblem(err, ['name', 'slug']);
          setErrors({
            name: mapped.fields.name,
            slug: mapped.fields.slug,
            form: mapped.formLevel.length > 0 ? mapped.formLevel.join(' ') : undefined,
          });
        },
      },
    );
  }

  return (
    <div className="mx-auto max-w-lg">
      <PageHeader title="Create workspace" description="A workspace owns its scenarios, keys, and streams." />
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        {errors.form && (
          <p role="alert" className="rounded-md bg-danger/10 px-3 py-2 text-sm text-danger">
            {errors.form}
          </p>
        )}
        <FormField label="Name" error={errors.name} required>
          {(p) => (
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
              {...p}
            />
          )}
        </FormField>
        <FormField
          label="Slug"
          error={errors.slug}
          hint="Used in URLs. Auto-derived from the name; edit if you like."
        >
          {(p) => (
            <Input
              value={effectiveSlug}
              onChange={(e) => {
                setSlugEdited(true);
                setSlug(e.target.value);
              }}
              autoComplete="off"
              {...p}
            />
          )}
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={() => void navigate(-1)}>
            Cancel
          </Button>
          <Button type="submit" loading={create.isPending} disabled={name.trim() === ''}>
            Create workspace
          </Button>
        </div>
      </form>
    </div>
  );
}
