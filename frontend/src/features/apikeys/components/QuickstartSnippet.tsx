import { CodeSnippet } from '../../../shared/ui';

export interface QuickstartSnippetProps {
  /** The plaintext key (reveal-once context) or a `df_<env>_<prefix>_…` placeholder. */
  apiKey: string;
  /** A stream id to template into the loop; a placeholder when none exists yet. */
  streamId?: string;
}

/**
 * QuickstartSnippet (frontend-architecture §9.6). The documented curl cursor loop
 * (PRD §2.1 day-1 journey) templated with the new key + a stream id. Rendered
 * inside the reveal-once dialog while the plaintext is still in local state; the
 * key never persists beyond that dialog (INV-TEN-4).
 */
export function QuickstartSnippet({ apiKey, streamId }: QuickstartSnippetProps) {
  const stream = streamId ?? '<STREAM_ID>';
  const code = [
    `# Pull events with cursor paging (api-specification §4.9)`,
    `CURSOR=""`,
    `while :; do`,
    `  RESP=$(curl -s "$DATAFORGE_API/api/v1/streams/${stream}/events?cursor=$CURSOR" \\`,
    `    -H "X-API-Key: ${apiKey}")`,
    `  echo "$RESP" | jq -c '.data[]'`,
    `  CURSOR=$(echo "$RESP" | jq -r '.next_cursor')`,
    `  sleep 1`,
    `done`,
  ].join('\n');

  return <CodeSnippet code={code} language="curl" />;
}
