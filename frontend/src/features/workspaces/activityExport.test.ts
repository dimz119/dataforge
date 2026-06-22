import { describe, expect, it } from 'vitest';

import type { AuditEntry } from '../../shared/api/types';
import {
  activityExportFilename,
  activityToCsv,
  activityToJson,
  buildActivityExport,
} from './activityExport';

function entry(over: Partial<AuditEntry> = {}): AuditEntry {
  return {
    audit_id: 'a-1',
    occurred_at: '2026-06-21T10:00:00.000Z',
    action: 'streams.stream.system_paused',
    actor: { type: 'system' },
    workspace_id: 'w-1',
    target: { type: 'stream', id: 's-1' },
    metadata: { reason: 'quota' },
    request_id: 'r-1',
    ...over,
  };
}

describe('activityExport', () => {
  it('serializes entries to CSV with a header row and flattened actor/target', () => {
    const csv = activityToCsv([entry({ actor: { email: 'admin@acme.test' } })]);
    const [header, row] = csv.split('\r\n');
    expect(header).toBe('audit_id,occurred_at,action,actor,target,workspace_id,request_id');
    expect(row).toContain('admin@acme.test');
    expect(row).toContain('stream:s-1');
    expect(row).toContain('streams.stream.system_paused');
  });

  it('quotes CSV cells containing commas or quotes (RFC 4180)', () => {
    const csv = activityToCsv([entry({ action: 'weird,"action"' })]);
    expect(csv.split('\r\n')[1]).toContain('"weird,""action"""');
  });

  it('emits null workspace_id / request_id as empty cells', () => {
    const csv = activityToCsv([entry({ workspace_id: null, request_id: null })]);
    const row = csv.split('\r\n')[1];
    expect(row.endsWith(',,')).toBe(true);
  });

  it('serializes entries to pretty JSON that round-trips', () => {
    const entries = [entry(), entry({ audit_id: 'a-2' })];
    const json = activityToJson(entries);
    expect(JSON.parse(json)).toEqual(entries);
    expect(json).toContain('\n'); // pretty-printed
  });

  it('builds the right mime type per format', () => {
    expect(buildActivityExport([], 'csv').mime).toContain('text/csv');
    expect(buildActivityExport([], 'json').mime).toContain('application/json');
  });

  it('stamps the filename with slug, date, and extension', () => {
    const name = activityExportFilename('acme', 'csv');
    expect(name).toMatch(/^activity-acme-\d{4}-\d{2}-\d{2}\.csv$/);
  });
});
