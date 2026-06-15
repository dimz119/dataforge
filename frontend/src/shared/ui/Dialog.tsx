import * as RadixDialog from '@radix-ui/react-dialog';
import type { ReactNode } from 'react';

import { cn } from '../lib/cn';

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  /** Footer actions row (right-aligned). */
  footer?: ReactNode;
  className?: string;
}

/**
 * Accessible modal dialog (frontend-architecture §8). Built on Radix Dialog for
 * focus trapping, `Escape`-to-close, scroll lock, and `aria-modal` semantics. The
 * CreateKeyDialog / RevealOnceDialog (§9.6), member-invite, and confirm dialogs
 * compose this. Closing is fully controlled so the reveal-once dialog can gate it
 * behind a confirm (INV-TEN-4).
 */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  className,
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm" />
        <RadixDialog.Content
          className={cn(
            'fixed left-1/2 top-1/2 z-50 w-[min(32rem,calc(100vw-2rem))] -translate-x-1/2',
            '-translate-y-1/2 rounded-lg border border-border bg-surface p-5 shadow-xl',
            'focus:outline-none',
            className,
          )}
        >
          <RadixDialog.Title className="text-base font-semibold text-text">
            {title}
          </RadixDialog.Title>
          {description && (
            <RadixDialog.Description className="mt-1 text-sm text-text-muted">
              {description}
            </RadixDialog.Description>
          )}
          {children && <div className="mt-4">{children}</div>}
          {footer && <div className="mt-6 flex justify-end gap-2">{footer}</div>}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
