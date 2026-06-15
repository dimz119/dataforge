import { forwardRef, type ButtonHTMLAttributes } from 'react';

import { cn } from '../lib/cn';

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
type Size = 'sm' | 'md';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  /** Renders a spinner and disables the button (mutation-pending state, §10.2). */
  loading?: boolean;
}

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-50',
  secondary:
    'bg-surface text-text border border-border hover:bg-surface-muted disabled:opacity-50',
  danger: 'bg-danger text-danger-fg hover:opacity-90 disabled:opacity-50',
  ghost: 'bg-transparent text-text hover:bg-surface-muted disabled:opacity-50',
};

const SIZES: Record<Size, string> = {
  sm: 'h-8 px-3 text-sm',
  md: 'h-10 px-4 text-sm',
};

/**
 * The shared button (frontend-architecture §8). Pending state shows an inline
 * spinner and disables interaction (§10.2 mutation-level loading). Keyboard and
 * focus behavior are the native button defaults; the global focus-visible ring
 * comes from theme.css.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', loading = false, disabled, className, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled ?? loading}
      aria-busy={loading || undefined}
      className={cn(
        'inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors',
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading && (
        <span
          aria-hidden="true"
          className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      {children}
    </button>
  );
});
