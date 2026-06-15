import { useCallback, useState } from 'react';

import { Button } from './Button';
import { cn } from '../lib/cn';

export interface CopyFieldProps {
  value: string;
  /** When set, the displayed text differs from the copied value (e.g. masked keys). */
  display?: string;
  /** Render the value with a monospace font (api keys, hashes, cursors). */
  mono?: boolean;
  label?: string;
  className?: string;
}

async function writeClipboard(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* fall through */
  }
  return false;
}

/**
 * Click-to-copy field with confirmation (frontend-architecture §8). The reveal-once
 * key dialog (§9.6) and the PinSummary sha256 (§9.5) use this. The value is a
 * plain text node — never HTML.
 */
export function CopyField({ value, display, mono = true, label, className }: CopyFieldProps) {
  const [copied, setCopied] = useState(false);

  const onCopy = useCallback(() => {
    void writeClipboard(value).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }, [value]);

  return (
    <div
      className={cn(
        'flex items-center gap-2 rounded-md border border-border bg-surface-muted px-3 py-2',
        className,
      )}
    >
      <code
        data-testid="copy-field-value"
        className={cn('min-w-0 flex-1 truncate text-sm text-text', mono && 'font-mono')}
      >
        {display ?? value}
      </code>
      <Button
        variant="secondary"
        size="sm"
        onClick={onCopy}
        aria-label={label ? `Copy ${label}` : 'Copy to clipboard'}
      >
        {copied ? 'Copied' : 'Copy'}
      </Button>
    </div>
  );
}
