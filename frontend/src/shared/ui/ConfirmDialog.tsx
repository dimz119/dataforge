import type { ReactNode } from 'react';

import { Button } from './Button';
import { Dialog } from './Dialog';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  /** Body content above the action row (e.g. an enumerated cascade). */
  children?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  /** Destructive styling for the confirm button. */
  danger?: boolean;
  /** Disable confirm (e.g. typed-confirmation not yet satisfied). */
  confirmDisabled?: boolean;
  loading?: boolean;
  onConfirm: () => void;
}

/**
 * Confirmation dialog (frontend-architecture §9.3/§9.6). Used for member removal,
 * key revoke, and reveal-once "close without copying". The workspace DangerZone
 * uses its own typed-confirmation variant; this is the plain yes/no form.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  confirmLabel,
  cancelLabel = 'Cancel',
  danger = false,
  confirmDisabled = false,
  loading = false,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={description}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={loading}>
            {cancelLabel}
          </Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            onClick={onConfirm}
            loading={loading}
            disabled={confirmDisabled}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}
