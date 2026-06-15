import type { ReactNode } from 'react';

import { Skeleton } from './Skeleton';
import { cn } from '../lib/cn';

export interface Column<Row> {
  /** Stable column id (also the header `scope=col` cell key). */
  id: string;
  header: ReactNode;
  /** Cell renderer for a row. */
  cell: (row: Row) => ReactNode;
  /** Right-align numeric columns. */
  align?: 'left' | 'right';
  className?: string;
}

export interface DataTableProps<Row> {
  columns: Column<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string;
  /** Click-through (e.g. row → detail); makes the row keyboard-activatable. */
  onRowClick?: (row: Row) => void;
  isLoading?: boolean;
  /** Rendered in place of the table body when there are no rows and not loading. */
  empty?: ReactNode;
  caption?: string;
  className?: string;
}

/**
 * Sortable-ready, accessible data table (frontend-architecture §8). Headers are
 * `<th scope="col">`; clickable rows are keyboard-activatable. Cursor-paginated
 * footers (DataTable "cursor-paginated footer", §8) are composed by the caller.
 */
export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  isLoading,
  empty,
  caption,
  className,
}: DataTableProps<Row>) {
  if (isLoading) {
    return (
      <div className="rounded-lg border border-border bg-surface p-4">
        <Skeleton lines={5} className="h-9" />
      </div>
    );
  }
  if (rows.length === 0 && empty) return <>{empty}</>;

  return (
    <div className={cn('overflow-x-auto rounded-lg border border-border bg-surface', className)}>
      <table className="w-full border-collapse text-sm">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
            {columns.map((c) => (
              <th
                key={c.id}
                scope="col"
                className={cn('px-3 py-2 font-medium', c.align === 'right' && 'text-right')}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const clickable = Boolean(onRowClick);
            return (
              <tr
                key={rowKey(row)}
                className={cn(
                  'border-b border-border last:border-0',
                  clickable && 'cursor-pointer hover:bg-surface-muted focus-within:bg-surface-muted',
                )}
                onClick={clickable ? () => onRowClick?.(row) : undefined}
              >
                {columns.map((c, ci) => (
                  <td
                    key={c.id}
                    className={cn('px-3 py-2 text-text', c.align === 'right' && 'text-right', c.className)}
                  >
                    {clickable && ci === 0 ? (
                      <button
                        type="button"
                        className="text-left outline-none"
                        onClick={(e) => {
                          e.stopPropagation();
                          onRowClick?.(row);
                        }}
                      >
                        {c.cell(row)}
                      </button>
                    ) : (
                      c.cell(row)
                    )}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
