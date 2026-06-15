/**
 * Minimal className combiner: filters falsy values and joins. No clsx dependency
 * — the conditional-class surface in the MVP is small and this keeps the bundle
 * lean (§12 budgets).
 */
export type ClassValue = string | false | null | undefined;

export function cn(...values: ClassValue[]): string {
  return values.filter(Boolean).join(' ');
}
