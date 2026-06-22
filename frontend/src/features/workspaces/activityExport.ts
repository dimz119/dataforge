/**
 * Activity-log export (frontend-architecture §9.3 / Phase 11). The audit endpoint has
 * no server-side export, so we serialize the already-fetched entries client-side. Pure
 * functions (no DOM) so they unit-test directly; `triggerActivityDownload` wires them
 * to a Blob download in the browser.
 */
import type { AuditEntry } from '../../shared/api/types';

export type ActivityExportFormat = 'csv' | 'json';

/** The flat columns surfaced in the CSV export (the audit-entry headline fields). */
const CSV_COLUMNS = [
  'audit_id',
  'occurred_at',
  'action',
  'actor',
  'target',
  'workspace_id',
  'request_id',
] as const;

/** Render an actor/target object to a stable, human-legible cell value. */
function describe(value: Record<string, unknown> | null | undefined): string {
  if (!value || typeof value !== 'object') return '';
  const email = value.email;
  if (typeof email === 'string') return email;
  const type = value.type;
  const id = value.id;
  const parts = [typeof type === 'string' ? type : undefined, typeof id === 'string' ? id : undefined];
  return parts.filter(Boolean).join(':');
}

/** RFC-4180 quote: wrap in quotes and double any embedded quote when needed. */
function csvCell(value: string): string {
  if (/[",\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

/** Serialize audit entries to a CSV string (headline columns; objects flattened). */
export function activityToCsv(entries: AuditEntry[]): string {
  const header = CSV_COLUMNS.join(',');
  const rows = entries.map((e) => {
    const cells: Record<(typeof CSV_COLUMNS)[number], string> = {
      audit_id: e.audit_id,
      occurred_at: e.occurred_at,
      action: e.action,
      actor: describe(e.actor),
      target: describe(e.target),
      workspace_id: e.workspace_id ?? '',
      request_id: e.request_id ?? '',
    };
    return CSV_COLUMNS.map((c) => csvCell(cells[c])).join(',');
  });
  return [header, ...rows].join('\r\n');
}

/** Serialize audit entries to a pretty-printed JSON array (the full, lossless shape). */
export function activityToJson(entries: AuditEntry[]): string {
  return JSON.stringify(entries, null, 2);
}

/** The filename for an export, stamped with the workspace slug + the current date. */
export function activityExportFilename(slug: string, format: ActivityExportFormat): string {
  const date = new Date().toISOString().slice(0, 10);
  return `activity-${slug}-${date}.${format}`;
}

/** Build the serialized payload + mime type for a format (used by tests + the button). */
export function buildActivityExport(
  entries: AuditEntry[],
  format: ActivityExportFormat,
): { content: string; mime: string } {
  if (format === 'csv') {
    return { content: activityToCsv(entries), mime: 'text/csv;charset=utf-8' };
  }
  return { content: activityToJson(entries), mime: 'application/json;charset=utf-8' };
}

/**
 * Trigger a browser download of the activity log in the chosen format. Follows the
 * Blob-anchor pattern used by the chaos answer-key export (chaos/api.ts) so the
 * download UX is consistent across the console.
 */
export function triggerActivityDownload(
  entries: AuditEntry[],
  slug: string,
  format: ActivityExportFormat,
): void {
  const { content, mime } = buildActivityExport(entries, format);
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement('a');
  a.href = url;
  a.download = activityExportFilename(slug, format);
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
