import { forwardRef, type InputHTMLAttributes } from 'react';

import { cn } from '../lib/cn';

export type InputProps = InputHTMLAttributes<HTMLInputElement> & {
  /** Marks the field invalid for a11y + styling (wired by FormField). */
  invalid?: boolean;
};

/**
 * Base text input (frontend-architecture §8). Always pairs with a `<label>` via
 * `FormField`; never renders a label itself. `aria-invalid` is set so screen
 * readers and the invalid ring agree.
 */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid, className, ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        'h-10 w-full rounded-md border bg-surface px-3 text-sm text-text',
        'placeholder:text-text-muted focus:outline-none',
        invalid ? 'border-danger' : 'border-border',
        className,
      )}
      {...rest}
    />
  );
});
