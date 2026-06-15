import { useCallback, useState } from 'react';

import { Button } from './Button';
import { cn } from '../lib/cn';

export interface CodeSnippetProps {
  code: string;
  /** Shown as a small label above the block (e.g. "curl"). */
  language?: string;
  className?: string;
}

/**
 * Read-only code block with copy (frontend-architecture §8). Used for the
 * `QuickstartSnippet` curl cursor loop (§9.6). Content is a text node — `<code>`
 * never receives HTML (XSS posture, §6.1).
 */
export function CodeSnippet({ code, language, className }: CodeSnippetProps) {
  const [copied, setCopied] = useState(false);

  const onCopy = useCallback(() => {
    if (!navigator.clipboard?.writeText) return;
    void navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }, [code]);

  return (
    <div className={cn('overflow-hidden rounded-md border border-border bg-surface-muted', className)}>
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="text-xs font-medium text-text-muted">{language ?? 'shell'}</span>
        <Button variant="ghost" size="sm" onClick={onCopy} aria-label="Copy snippet">
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <pre className="overflow-x-auto p-3 text-xs leading-relaxed text-text">
        <code className="font-mono">{code}</code>
      </pre>
    </div>
  );
}
