# ADR-0013 — REST = replayable cursor over a time-partitioned Postgres buffer; WS = best-effort tail via Channels in a separate ASGI process

**Deliverable:** D17

DataForge's two MVP delivery channels get deliberately asymmetric semantics: REST is an at-least-once, replayable, client-paced cursor over a time-partitioned Postgres buffer, and WebSocket is an at-most-once live tail served by Django Channels in its own ASGI process group. This warranted an ADR because the channel semantics are the user-visible contract of the whole data plane — once consumers exist, changing replay, ordering, or expiry behavior breaks every pipeline built against DataForge, and the per-channel guarantee table in [../03-domain/event-model.md](../03-domain/event-model.md) §6 is published verbatim to users.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** the MVP consumption surface (Phases 5–6) and the semantics baseline every later channel is contract-tested against (Phase 12); buffer persistence shape; WS process placement

## Context

The forces:

- **The consumption-model boundary (user-confirmed, binding):** MVP users consume hosted DataForge over the internet via cursor-based REST and WebSocket with an API key; internal Kafka is never exposed (ADR-0005, CB-1 in [../02-architecture/system-architecture.md](../02-architecture/system-architecture.md) §5). So REST/WS are not stopgaps — they are the product's primary channels and must have production-grade, *teachable* semantics.
- **Replay is pedagogy.** The SWE persona "deliberately replays a cursor and observes at-least-once duplicates" (PRD §2.5); exercise E1 grades dedup against a re-readable stream. Replay-stability — re-reading a cursor returns identical events in identical order — is INV-DEL-3.
- **Panel gap, must be closed here:** all three proposals used a TTL'd buffer with replayable cursors, but none defined what happens when a cursor points into an expired partition. Silent skip would be a correctness lie to learners.
- **Retention is a cost and quota lever:** 24 h (Free) / 48 h (Classroom, Pro) per PRD §7, which demands O(1) expiry — per-row deletes at target TPS would dominate the database.
- **WS must not become the bulk path:** sockets are stateful, per-connection backpressure is poor at high TPS, and the console tail at 100+ TPS must not freeze (Phase 7 exit criterion). The panel's resolved disagreement on WS placement: P1 argued explicitly for a separate ASGI process group; others were looser.

## Decision

1. **Buffer materialization.** A buffer-writer sink (Kafka consumer group `df.sink.rest-buffer.v1`, the first `DeliveryChannel` implementation) consumes post-chaos events from internal topics, calls `strip_internal()` at ingest (SB-2), and writes batches (≤ 500 events / 250 ms) to a **time-partitioned Postgres event buffer** in buffer-append order. Kafka offsets are committed only after the insert transaction — at-least-once into a replay-stable store ([../02-architecture/backend-architecture.md](../02-architecture/backend-architecture.md) §8.6). Buffer rows store the delivered shape exactly, which is what makes replay byte-stable per channel (event-model §5.2).
2. **REST cursor pull** (`GET /api/v1/streams/{stream_id}/events`, API-key authenticated, scope `events:read`): client-paced, at-least-once, totally ordered per stream in buffer-append order. Cursors are opaque URL-safe tokens (clients must never parse them — domain model §6.1); re-reading any cursor within retention returns identical events (INV-DEL-3).
3. **Retention by partition drop.** Buffer partitions are dropped whole when they age out of the plan's retention window (24–48 h wall time). **A cursor pointing into a dropped partition fails explicitly with HTTP `410 Gone`, problem type `cursor-expired` (RFC 9457, ADR-0014) — never a silent skip to the oldest retained event** (INV-DEL-4). The error is documented as a teaching moment: it is exactly what consumers face when they fall behind a real retention window.
4. **WebSocket live tail** (`/ws/streams/{stream_id}/events`): Django Channels on the Redis channel layer, delivered **at-most-once per connection** (INV-DEL-5). A `ws-pusher` sink (consumer group `df.sink.websocket.v1`) bridges Kafka to channel groups with a per-stream monotonic `frame_seq`; channel-layer overflow drops oldest frames and the consumer emits an explicit drop-notice frame with the dropped count — degradation is visible, never silent. The subprotocol is versioned, and **resume-from-cursor hands off to REST semantics**: the socket carries no replay state of its own. WS is a tail/debug channel, never the bulk-throughput path.
5. **Dedicated ASGI process group.** WS runs as the `ws` process group (uvicorn/Channels), separate from the WSGI `web` group (ADR-0015). The REST tier stays stateless and WSGI; WS capacity scales independently; a WS-tier incident cannot take down the control plane.
6. **Published guarantees.** The per-channel ordering/lateness/duplicate table (event-model §6) is the user contract; future channels (external Kafka, webhooks, S3/Iceberg) are held to their frozen rows by the cross-channel contract suite.

The asymmetry at a glance:

| Property | REST cursor pull | WebSocket tail |
|---|---|---|
| Delivery semantics | At-least-once, client-paced | At-most-once per connection |
| Store / transport | Time-partitioned Postgres buffer | Redis channel layer (no store) |
| Ordering | Buffer-append order, replay-stable (INV-DEL-3) | Same order minus dropped frames (signaled) |
| Replay | Any cursor within 24–48 h retention; `410 cursor-expired` beyond (INV-DEL-4) | None on the socket; resume-from-cursor → REST |
| Backpressure | Client paces itself | Drop-oldest + explicit drop-notice frame (INV-DEL-5) |
| Role | Completeness, bulk, grading input | Liveness, console tail, debugging |
| Process group | `web` (WSGI, stateless) | `ws` (ASGI Channels, dedicated) |

## Alternatives considered

- **Serve REST pulls directly from Kafka** (per-request consumer, offset-based cursors). Rejected: it couples the user-facing replay window to broker retention (deliberately short — 6 h, [../02-architecture/deployment-architecture.md](../02-architecture/deployment-architecture.md) §3.4), leaks offset semantics into the public API, makes per-workspace authorization and RLS-backed isolation (ADR-0002) a custom broker-side problem instead of a solved Postgres one, and puts tenant reads on the single MVP broker. The buffer keeps Kafka internal-only (CB-1) and gives replay SQL-grade query semantics inside the existing tenancy walls.
- **Redis as the buffer store.** Rejected: 24–48 h of events at plan TPS caps is memory-priced storage with no partition-drop economics; the shared Redis is mandated `noeviction` for correctness-bearing state (deployment architecture §3.5), so a large evictable dataset cannot share it; durability across restarts is the point of the buffer.
- **WebSocket as the primary/bulk channel** (push everything, REST as an afterthought). Rejected: per-socket backpressure inverts control of pacing, replay over a socket requires reinventing the cursor anyway, and stateful connections fight the stateless-API NFR. The chosen split gives each channel one job: REST = completeness and replay; WS = liveness.
- **Silent cursor reset to oldest-retained on expiry.** Rejected per the panel gap: it silently hides data loss from the consumer — the opposite of what a data-engineering teaching product should model. The explicit `410` makes retention a first-class, observable contract.
- **WS in the same process/tier as REST** (one ASGI deployment serving both). Rejected per the resolved panel disagreement: P1's separate-process argument won because it keeps the REST tier stateless and the WS tier independently scalable at zero design cost; mixing them couples socket-heavy memory/connection load to control-plane availability.

## Consequences

### Positive

- At-least-once, replay-stable REST makes dedup, idempotency, and retention real, gradable exercises rather than documentation claims (PRD E1, §2.5).
- Partition drop makes retention O(1) regardless of volume and maps plan tiers (PRD §7) onto a single mechanical knob.
- The explicit `cursor-expired` contract converts the nastiest edge case (client falls behind retention) into a documented, testable behavior — INV-DEL-4 is a permanent test target.
- Channel asymmetry is honest: nobody is tempted to build a bulk consumer on WS, and the drop-notice frame keeps at-most-once visible.

### Negative

- Every delivered event is written twice (Kafka, then Postgres buffer): buffer ingest is a named bottleneck rung in [../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md), with batch inserts and time partitioning as the committed remedies.
- REST pull latency is poll-bounded; consumers wanting sub-second liveness use the WS tail or wait for Phase 12 hosted Kafka topics.
- A WS connection can never prove completeness — by design; the docs must (and do) route completeness claims to REST/answer-key surfaces.

### Follow-ups

- [../04-engines/delivery-channels.md](../04-engines/delivery-channels.md): the `DeliveryChannel` interface both sinks implement; WS subprotocol versioning, frame shapes, drop-notice format; resume-from-cursor handoff.
- [../05-interfaces/api-specification.md](../05-interfaces/api-specification.md): events endpoint parameters, cursor token contract, `cursor-expired` and rate-limit problem types, WS close codes.
- [../03-domain/database-schema.md](../03-domain/database-schema.md): buffer DDL, partition window sizing, drop job, RLS policies on buffer rows.
- [../06-quality/testing-strategy.md](../06-quality/testing-strategy.md): replay-stability tests (INV-DEL-3), expired-cursor tests (INV-DEL-4), drop-notice tests (INV-DEL-5); Phase 6 soak comparing WS and REST content.
