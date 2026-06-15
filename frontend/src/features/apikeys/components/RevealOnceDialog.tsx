import { useEffect, useState } from 'react';

import type { ApiKeyCreated } from '../../../shared/api/types';
import { Button, ConfirmDialog, CopyField, Dialog } from '../../../shared/ui';
import { QuickstartSnippet } from './QuickstartSnippet';

export interface RevealOnceDialogProps {
  /** The 201 create result, or null when the dialog is closed. */
  created: ApiKeyCreated | null;
  /** Called when the dialog fully closes; the parent nulls out `created`. */
  onClose: () => void;
  /** Optional stream id to template into the quickstart snippet. */
  streamId?: string;
}

/**
 * RevealOnceDialog (frontend-architecture §9.6, INV-TEN-4).
 *
 * The plaintext key lives in dialog-LOCAL `useState` only — it is copied out of
 * the mutation result on open and CLEARED on close, so it is never persisted and
 * never present in the DOM after the dialog closes. The mutation result itself
 * never enters the Query cache (see api.ts). Closing without copying requires an
 * explicit confirm, since the secret cannot be retrieved again.
 */
export function RevealOnceDialog({ created, onClose, streamId }: RevealOnceDialogProps) {
  // Dialog-local plaintext — the ONLY place the secret lives (INV-TEN-4).
  const [plaintext, setPlaintext] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmingClose, setConfirmingClose] = useState(false);

  // Seed local state when a fresh key arrives; never store it anywhere else.
  useEffect(() => {
    if (created) {
      setPlaintext(created.key);
      setCopied(false);
      setConfirmingClose(false);
    }
  }, [created]);

  const open = created !== null;

  function finalize() {
    // Wipe the secret from local state, then notify the parent.
    setPlaintext(null);
    setCopied(false);
    setConfirmingClose(false);
    onClose();
  }

  function requestClose() {
    if (copied) finalize();
    else setConfirmingClose(true);
  }

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(o) => {
          if (!o) requestClose();
        }}
        title="API key created"
        description="This key is shown once — DataForge stores only a hash. Copy it now."
        footer={
          <Button variant="secondary" onClick={requestClose}>
            Done
          </Button>
        }
      >
        {plaintext && (
          <div className="flex flex-col gap-4">
            <CopyField
              value={plaintext}
              label="API key"
              className="border-accent/50"
            />
            <button
              type="button"
              onClick={() => setCopied(true)}
              className="self-start text-xs text-accent hover:underline"
            >
              I have copied this key
            </button>
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-text-muted">
                Quickstart
              </p>
              <QuickstartSnippet apiKey={plaintext} streamId={streamId} />
            </div>
          </div>
        )}
      </Dialog>

      <ConfirmDialog
        open={confirmingClose}
        onOpenChange={(o) => {
          if (!o) setConfirmingClose(false);
        }}
        title="Close without copying?"
        description="The key cannot be shown again. If you have not copied it, you will need to create a new one."
        confirmLabel="Close anyway"
        cancelLabel="Keep open"
        danger
        onConfirm={finalize}
      />
    </>
  );
}
