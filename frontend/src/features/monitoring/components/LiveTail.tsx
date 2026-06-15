import { useVirtualizer } from '@tanstack/react-virtual';
import { useEffect, useRef, useState } from 'react';

import { Button, EmptyState, JsonViewer } from '../../../shared/ui';
import { cn } from '../../../shared/lib/cn';
import { useStreamTail, type WsTransportFactory } from '../../../shared/ws';
import { EventTypeFilter } from './EventTypeFilter';
import { SamplingBadge } from './SamplingBadge';
import { TailRow } from './TailRow';

export interface LiveTailProps {
  streamId: string;
  /** Stream status, for the empty-state hint when no events are arriving. */
  streamStatus: string;
  /** Known event types for the filter (from the pinned manifest / stats). */
  knownTypes?: string[];
  /** Terminal close (4403/4404) callback so the page can render NotFound. */
  onTerminal?: () => void;
  /** TEST SEAM ONLY: forwarded to `useStreamTail` to substitute a FakeTailSocket. */
  transportFactory?: WsTransportFactory;
}

const STATUS_DOT: Record<string, string> = {
  connecting: 'bg-status-amber',
  open: 'bg-status-green',
  reconnecting: 'bg-status-amber df-pulse',
  closed: 'bg-status-gray',
};

const ROW_ESTIMATE = 36;

/**
 * The live event tail (frontend-architecture §9.7, §7.6). `useStreamTail` (sampled,
 * 4 Hz-batched ring buffer) feeds a `@tanstack/react-virtual` list so the DOM node
 * count stays bounded under a flood (the Phase 7 exit criterion: no freeze at 100+
 * TPS). Toolbar: event-type filter, display pause, clear, SamplingBadge, status dot.
 * Inline notice rows render below the newest events.
 */
export function LiveTail({
  streamId,
  streamStatus,
  knownTypes,
  onTerminal,
  transportFactory,
}: LiveTailProps) {
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [paused, setPaused] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);

  const tail = useStreamTail(streamId, {
    eventTypes: selectedTypes.length ? selectedTypes : undefined,
    displayPaused: paused,
    transportFactory,
  });

  useEffect(() => {
    if (tail.terminal) onTerminal?.();
  }, [tail.terminal, onTerminal]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  const events = tail.events;
  const virtualizer = useVirtualizer({
    count: events.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_ESTIMATE,
    overscan: 12,
  });

  // Auto-scroll to newest while pinned; a manual scroll-up disengages it (§7.6).
  useEffect(() => {
    if (stickToBottom.current && events.length > 0) {
      virtualizer.scrollToIndex(events.length - 1, { align: 'end' });
    }
  }, [events.length, virtualizer]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottom.current = distanceFromBottom < 48;
  };

  const jumpToLive = () => {
    stickToBottom.current = true;
    if (events.length > 0) virtualizer.scrollToIndex(events.length - 1, { align: 'end' });
  };

  const virtualItems = virtualizer.getVirtualItems();

  return (
    <section aria-label="Live event tail" className="flex flex-col rounded-lg border border-border bg-surface">
      <Toolbar
        status={tail.status}
        counters={tail.counters}
        sampling={tail.sampling}
        knownTypes={knownTypes ?? []}
        selectedTypes={selectedTypes}
        onTypesChange={setSelectedTypes}
        paused={paused}
        onTogglePause={() => setPaused((p) => !p)}
        onClear={() => {
          setExpanded(null);
          tail.clear();
        }}
      />

      <NoticeStack notices={tail.notices} />

      {events.length === 0 ? (
        <EmptyState
          className="m-4"
          title={tail.status === 'open' ? 'Connected — waiting for events' : 'Connecting…'}
          description={
            streamStatus !== 'running'
              ? `This stream is ${streamStatus.replace(/_/g, ' ')}; start it to see events.`
              : undefined
          }
        />
      ) : (
        <div className="relative">
          <div
            ref={scrollRef}
            onScroll={onScroll}
            className="h-[28rem] overflow-auto"
            role="log"
            aria-live="off"
            aria-label="Event rows"
          >
            <div style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>
              {virtualItems.map((vi) => {
                const event = events[vi.index];
                return (
                  <div
                    key={vi.key}
                    data-index={vi.index}
                    ref={virtualizer.measureElement}
                    style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${vi.start}px)` }}
                  >
                    <TailRow
                      event={event}
                      expanded={expanded === vi.index}
                      onToggle={() => setExpanded((cur) => (cur === vi.index ? null : vi.index))}
                    />
                    {expanded === vi.index && (
                      <div className="border-b border-border bg-surface-muted px-3 py-2">
                        <JsonViewer value={event} defaultExpandDepth={2} />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
          {!stickToBottom.current && (
            <Button
              size="sm"
              className="absolute bottom-3 right-3 shadow"
              onClick={jumpToLive}
            >
              ↓ live
            </Button>
          )}
        </div>
      )}
    </section>
  );
}

interface ToolbarProps {
  status: ReturnType<typeof useStreamTail>['status'];
  counters: ReturnType<typeof useStreamTail>['counters'];
  sampling: ReturnType<typeof useStreamTail>['sampling'];
  knownTypes: string[];
  selectedTypes: string[];
  onTypesChange: (types: string[]) => void;
  paused: boolean;
  onTogglePause: () => void;
  onClear: () => void;
}

function Toolbar({
  status,
  counters,
  sampling,
  knownTypes,
  selectedTypes,
  onTypesChange,
  paused,
  onTogglePause,
  onClear,
}: ToolbarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
      <span className="inline-flex items-center gap-1.5 text-xs font-medium text-text-muted">
        <span className={cn('h-2 w-2 rounded-full', STATUS_DOT[status])} aria-hidden />
        {status}
      </span>
      {knownTypes.length > 0 && (
        <EventTypeFilter
          types={knownTypes}
          selected={selectedTypes}
          onChange={onTypesChange}
        />
      )}
      <div className="ml-auto flex items-center gap-2">
        <SamplingBadge active={sampling.active} keepRatio={sampling.keepRatio} />
        <span className="text-xs tabular-nums text-text-muted" aria-live="polite">
          {counters.received.toLocaleString('en-US')} received (this connection)
        </span>
        <Button variant="secondary" size="sm" onClick={onTogglePause} aria-pressed={paused}>
          {paused ? 'Resume display' : 'Pause display'}
        </Button>
        <Button variant="ghost" size="sm" onClick={onClear}>
          Clear
        </Button>
      </div>
    </div>
  );
}

function NoticeStack({ notices }: { notices: ReturnType<typeof useStreamTail>['notices'] }) {
  // Show the most recent few inline notices (drops / cursor-expired / reconnect gaps).
  const recent = notices.slice(-3);
  if (recent.length === 0) return null;
  return (
    <ul className="divide-y divide-border border-b border-border">
      {recent.map((n) => (
        <li
          key={n.id}
          className={cn(
            'px-3 py-1.5 text-xs',
            n.kind === 'cursor-expired' ? 'bg-status-red/10 text-status-red' : 'bg-status-amber/10 text-status-amber',
          )}
        >
          {n.message}
        </li>
      ))}
    </ul>
  );
}
