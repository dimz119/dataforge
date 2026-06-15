import { type FormEvent, useState } from 'react';
import { useNavigate } from 'react-router';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import type { ScenarioDetail } from '../../../shared/api/types';
import { Button, Dialog, FormField, Input, useToast } from '../../../shared/ui';
import { useCreateInstance } from '../api';

export interface CreateInstanceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  workspaceSlug: string;
  scenario: ScenarioDetail;
}

/**
 * Create-instance flow (frontend-architecture §9.4). Name + version picker
 * (defaults to latest published, INV-CAT-5). Creates with the manifest config
 * DEFAULTS (no overlay), then routes to the InstanceConfigPage where the overlay
 * editor lives. Version selection is disabled when only one version exists.
 */
export function CreateInstanceDialog({
  open,
  onOpenChange,
  workspaceId,
  workspaceSlug,
  scenario,
}: CreateInstanceDialogProps) {
  const create = useCreateInstance(workspaceId);
  const toast = useToast();
  const navigate = useNavigate();

  const defaultVersion = scenario.latest_version ?? scenario.published_versions[0] ?? '';
  const [name, setName] = useState('');
  const [version, setVersion] = useState(defaultVersion);
  const [errors, setErrors] = useState<{ name?: string; form?: string }>({});

  function reset() {
    setName('');
    setVersion(defaultVersion);
    setErrors({});
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErrors({});
    create.mutate(
      {
        name,
        scenario_slug: scenario.scenario_slug,
        manifest_version: version,
        // Created with manifest defaults; the overlay editor lives on the config page.
      },
      {
        onSuccess: (instance) => {
          toast.show({ title: 'Instance created', tone: 'success' });
          reset();
          onOpenChange(false);
          void navigate(`/w/${workspaceSlug}/scenarios/instances/${instance.scenario_instance_id}`);
        },
        onError: (err) => {
          const mapped = mapValidationProblem(err, ['name', 'manifest_version']);
          setErrors({
            name: mapped.fields.name,
            form:
              mapped.formLevel.length > 0
                ? mapped.formLevel.join(' ')
                : mapped.fields.manifest_version,
          });
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
      title="Create instance"
      description={`A configured copy of ${scenario.title} you can drive streams from.`}
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
        <FormField label="Version" hint="Defaults to the latest published version.">
          {(p) => (
            <select
              id={p.id}
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              disabled={scenario.published_versions.length <= 1}
              className="h-10 w-full rounded-md border border-border bg-surface px-3 text-sm text-text disabled:opacity-60"
            >
              {scenario.published_versions.map((v) => (
                <option key={v} value={v}>
                  v{v}
                  {v === scenario.latest_version ? ' (latest)' : ''}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="submit"
            loading={create.isPending}
            disabled={name.trim() === '' || version === ''}
          >
            Create instance
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
