import { useId, type ReactNode } from 'react';

import { cn } from '../lib/cn';

export interface FormFieldProps {
  label: string;
  /** Field-level error text (from zod or a mapped problem pointer, §10.4). */
  error?: string;
  hint?: string;
  required?: boolean;
  /** Render-prop: receives the generated id + a11y props to spread on the control. */
  children: (props: {
    id: string;
    invalid: boolean;
    'aria-describedby': string | undefined;
  }) => ReactNode;
  className?: string;
}

/**
 * Accessible label/control/error wrapper (frontend-architecture §10.4). Generates
 * a stable id, wires `aria-describedby` to the hint/error, and surfaces the error
 * with `role="alert"` so it is announced. The control is supplied via render-prop
 * so any input/select can be wrapped.
 */
export function FormField({
  label,
  error,
  hint,
  required,
  children,
  className,
}: FormFieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  const describedBy = error ? errorId : hint ? hintId : undefined;

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <label htmlFor={id} className="text-sm font-medium text-text">
        {label}
        {required && <span className="text-danger"> *</span>}
      </label>
      {children({ id, invalid: Boolean(error), 'aria-describedby': describedBy })}
      {hint && !error && (
        <p id={hintId} className="text-xs text-text-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} role="alert" className="text-xs text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
