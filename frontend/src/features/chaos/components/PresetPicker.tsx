import { useState } from 'react';

import { Button, ConfirmDialog } from '../../../shared/ui';
import { CHAOS_PRESETS, presetToDocument, type ChaosPreset } from '../presets';
import { CHAOS_MODES, MODE_META, type ChaosPolicyDocument } from '../types';

export interface PresetPickerProps {
  current: ChaosPolicyDocument;
  /** Apply the expanded bundle to the panel form (the panel then PATCHes). */
  onApply: (next: ChaosPolicyDocument) => void;
}

/** Per-mode enable transitions the preset would cause, for the confirm diff. */
function diffModes(from: ChaosPolicyDocument, to: ChaosPolicyDocument): string[] {
  const lines: string[] = [];
  for (const mode of CHAOS_MODES) {
    const was = from[mode].enabled;
    const now = to[mode].enabled;
    if (was === now && from[mode].rate === to[mode].rate) continue;
    const label = MODE_META[mode].label;
    if (!was && now) lines.push(`enable ${label} (rate ${to[mode].rate.toFixed(2)})`);
    else if (was && !now) lines.push(`disable ${label}`);
    else lines.push(`${label} rate → ${to[mode].rate.toFixed(2)}`);
  }
  return lines;
}

/**
 * PresetPicker (frontend-architecture §9.5): pick a PRD exercise preset and apply it
 * as a bundle. Applying a preset REPLACES the whole document (chaos-engine §8), so we
 * show a confirm diff before handing the expanded doc to the panel form for PATCH.
 */
export function PresetPicker({ current, onApply }: PresetPickerProps) {
  const [pending, setPending] = useState<ChaosPreset | null>(null);
  const preview = pending ? presetToDocument(pending, current.on_stop_policy) : null;
  const changes = preview ? diffModes(current, preview) : [];

  return (
    <div className="space-y-2" role="group" aria-label="Exercise presets">
      <p className="text-sm font-medium text-text">Exercise presets</p>
      <div className="flex flex-wrap gap-2">
        {CHAOS_PRESETS.map((p) => (
          <Button
            key={p.slug}
            variant="secondary"
            size="sm"
            onClick={() => setPending(p)}
            title={p.description}
          >
            {p.name}
          </Button>
        ))}
      </div>

      <ConfirmDialog
        open={pending != null}
        onOpenChange={(open) => !open && setPending(null)}
        title={pending ? `Apply “${pending.name}”?` : ''}
        description={pending?.description}
        confirmLabel="Apply preset"
        onConfirm={() => {
          if (preview) onApply(preview);
          setPending(null);
        }}
      >
        <p className="mb-2 text-sm text-text-muted">
          This replaces the whole chaos policy (unlisted modes are disabled):
        </p>
        {changes.length > 0 ? (
          <ul className="list-disc space-y-1 pl-5 text-sm text-text">
            {changes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-text-muted">No changes — the policy already matches.</p>
        )}
      </ConfirmDialog>
    </div>
  );
}
