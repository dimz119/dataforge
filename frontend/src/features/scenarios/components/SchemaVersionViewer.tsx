import { useQuery } from '@tanstack/react-query';

import { CopyField, ErrorState, JsonViewer, Skeleton } from '../../../shared/ui';
import { formatRelativeTime } from '../../../shared/lib/relativeTime';
import { subjectVersionQueryOptions } from '../api';

export interface SchemaVersionViewerProps {
  workspaceId: string;
  subject: string;
  /** The selected version number. */
  version: number;
}

/** Read a string field off the loosely-typed schema document, else undefined. */
function docStr(doc: Record<string, unknown>, key: string): string | undefined {
  const v = doc[key];
  return typeof v === 'string' ? v : undefined;
}

/**
 * One schema-version inspector (frontend-architecture §9.4). Fetches the immutable
 * version record (#65; staleTime Infinity) and renders the `$id`, the canonical
 * `schema_ref` string (`{subject}/{version}`), the registered-at provenance, a copy
 * action for the full document, and a collapsible JsonViewer of the schema. The
 * JsonViewer renders every key/value as a text node (XSS-safe, T-8).
 */
export function SchemaVersionViewer({ workspaceId, subject, version }: SchemaVersionViewerProps) {
  const record = useQuery(subjectVersionQueryOptions(workspaceId, subject, String(version)));

  if (record.isPending) return <Skeleton lines={6} className="h-8" />;
  if (record.error) {
    return <ErrorState error={record.error} onRetry={() => void record.refetch()} />;
  }

  const doc = record.data.schema as Record<string, unknown>;
  const schemaRef = `${record.data.subject}/${String(record.data.version)}`;
  const id = docStr(doc, '$id');

  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-text">v{record.data.version} document</h3>
        <span className="text-xs text-text-muted">
          registered {formatRelativeTime(record.data.registered_at)}
        </span>
      </div>

      <dl className="space-y-2 text-xs">
        <div className="flex flex-col gap-1">
          <dt className="text-text-muted">schema_ref</dt>
          <dd>
            <code className="font-mono text-text">{schemaRef}</code>
          </dd>
        </div>
        {id && (
          <div className="flex flex-col gap-1">
            <dt className="text-text-muted">$id</dt>
            <dd>
              <code className="break-all font-mono text-text">{id}</code>
            </dd>
          </div>
        )}
      </dl>

      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <span className="text-xs text-text-muted">Schema document</span>
          <CopyField
            value={JSON.stringify(doc, null, 2)}
            display={schemaRef}
            label="schema document"
            mono
            className="max-w-xs"
          />
        </div>
        <JsonViewer value={doc} defaultExpandDepth={2} />
      </div>
    </div>
  );
}
