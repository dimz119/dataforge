# Phase 6 — Stream Control Surface: Pause/Resume, Dynamic TPS, WebSocket, Stats

**Deliverable:** D18 (phase doc)

Phase 6 completes the streaming control plane and live delivery: pause/resume with full actor-state checkpoint/restore (in-flight funnels survive), dynamic TPS while running, the WebSocket tail through a dedicated Channels ASGI process, and the per-stream stats surface. After this phase the entire MVP data-plane contract a consumer experiences — REST replay, WS tail, lifecycle, pacing, counters — is in place; Phase 7 only puts a face on it.

## Goal

> Complete the streaming control plane and live delivery.

## Dependencies

| Dependency | Role |
|---|---|
| Phase 5 complete | Streams, runners, leases, Kafka backbone, buffer + REST cursors |
| [../../04-engines/delivery-channels.md](../../04-engines/delivery-channels.md) §6 | WS contract (WS-1…WS-12): subprotocol `dataforge.events.v1`, first-message auth ≤ 10 s (else close 4408), close-code table, `resume_ack` + REST gap-fill (the socket never replays), drop-notice frames |
| [../../04-engines/behavior-engine.md](../../04-engines/behavior-engine.md) | Checkpoint/restore semantics (dwell-timer rebasing, RNG positions), token-bucket pacing |
| [../../03-domain/domain-model.md](../../03-domain/domain-model.md) §4 | T5–T9 pause/resume transitions, idempotency (INV-STR-3), live-mutable set (§4.4) |
| [../../02-architecture/backend-architecture.md](../../02-architecture/backend-architecture.md) | `ws` ASGI process group, ws-pusher sink (§8.6) in the runner sink host, Redis channel layer |
| [../../02-architecture/observability.md](../../02-architecture/observability.md) | StreamStats counter catalog, INV-OBS-2 (≤ 5 s staleness), rebuildability |
| [../../05-interfaces/api-specification.md](../../05-interfaces/api-specification.md) | Pause/resume/TPS endpoint shapes, WS frame catalog, stats response shape |
| ADR-0006 (reconciliation), ADR-0013 (WS = best-effort tail in a separate ASGI process) | Structural decisions implemented here |

## Scope

- **Pause/resume (T5–T8):** `POST /api/v1/streams/{id}/pause` and `/resume`, idempotent; pause halts emission within one tick and persists a checkpoint (lease retained); resume restores actor/session machines, rebases dwell timers to the frozen-then-resumed virtual clock, and continues in-flight funnels with zero integrity violations and zero canonical sequence gaps. `status_reason` plumbing (`user`/`quota`/`idle`/`error`) is wired now; quota/idle *triggers* arrive in Phase 11.
- **Dynamic TPS (1–1,000):** `PATCH /api/v1/streams/{id}` `{"target_tps": N}` while running, quota-capped at command time (INV-TEN-5); runner token-bucket pacing picks the new value up on its next desired-state poll — effective within 2 s; every change audit-logged.
- **WebSocket tail:** the `ws` ASGI process (Channels + Redis channel layer) serving `/ws/streams/{id}/events`; the ws-pusher sink (consumer group `df.sink.websocket.v1`) consumes post-chaos Kafka into per-stream channel groups (strip applied at sink ingest, SB-2); versioned subprotocol `dataforge.events.v1` (a handshake offering no supported subprotocol is rejected at HTTP level with `400`, WS-1); first-message auth frame `{"type":"auth","api_key":"df_…"}` or `{"type":"auth","access_token":"…"}` within 10 s else close `4408` (invalid/revoked credentials `4401`; missing scope `4403`; unknown or foreign-workspace stream `4404`; query-string credentials rejected — WS-2/WS-3); resume-from-cursor never replays on the socket — a `cursor` in `auth`/`resume` yields `resume_ack` with the `behind` gap and the client backfills over REST (WS-6/WS-7; an expired cursor sends a non-fatal `error` frame, WS-8); backpressure = drop-oldest with explicit drop-notice frames carrying counts (INV-DEL-5).
- **Stream stats:** Redis-resident, rebuildable counters per stream — `total_events`, `observed_tps`, per-event-type counts, `last_event_at` — updated on the sink path; `GET /api/v1/streams/{id}/stats`; staleness ≤ 5 s (INV-OBS-2); workspace/stream-labeled (INV-OBS-3).
- **Suites landing:** GOLD-D (stop/restart continuation byte-identity), OPS-4/5, XCH-1/2 cross-channel content harness, SOAK-200 nightly harness with independent REST + WS consumers; TEN P6 probes (foreign-workspace key on the WS handshake → close `4404`, foreign stats → 404).

## Non-goals

| Deferred | Lands in |
|---|---|
| Console UI for any of this | Phase 7 |
| Intensity curves, speed multiplier, backfill-mode streams (virtual clock pause/rebase ships now) | Phase 8 |
| Chaos toggles in the desired state (the live-mutable slot is reserved per domain-model §4.4) | Phase 9 |
| Quota/idle auto-pause triggers (the `paused_quota`/`paused_idle` statuses render from `status_reason` now) | Phase 11 |
| WS as a bulk-throughput path — never; drop-oldest is the contract (INV-DEL-5) | — (permanent posture) |
| Multi-shard ordering and the hardened backpressure policy (batch REST endpoint, lag handling) | Phase 11 |

## Tasks

- [ ] Pause command path: desired `paused`, runner one-tick halt, checkpoint persist, `pausing → paused` convergence
- [ ] Resume command path: checkpoint restore, dwell-timer rebase to virtual clock, `resuming → running`; idempotency tests for both
- [ ] Stop-override rule (T9: stop wins over in-flight pause/start) + GOLD-D continuation fixture
- [ ] `PATCH` `target_tps` API with quota command-time check + audit entry
- [ ] Token-bucket pacing in the runner tick loop; ≤ 2 s effectiveness stopwatch test (OPS-5)
- [ ] Redis channel layer config + `ws` ASGI process (replaces the Phase-1 stub command)
- [ ] ws-pusher sink (`df.sink.websocket.v1`): Kafka → channel-group fan-out with `frame_seq` behind the `DeliveryChannel` interface
- [ ] WS consumer: subprotocol negotiation (no supported subprotocol → HTTP `400`), auth frame ≤ 10 s (`4408`; invalid credentials `4401`; missing scope `4403`), stream-ownership check (foreign → `4404`), `ready` frame
- [ ] WS resume positioning: `resume_ack` with `behind {events, from_cursor}` gap reporting + REST gap-fill handoff (WS-6/WS-7); expired cursor → non-fatal `error` frame with `earliest_cursor` (WS-8)
- [ ] WS backpressure: bounded per-connection queue, drop-oldest, drop-notice frames with accurate counts (OPS-8)
- [ ] Stats counters on the sink path (Redis pipelines) + rebuild command from the buffer
- [ ] `GET /api/v1/streams/{id}/stats` endpoint + ≤ 5 s staleness assertion hooks
- [ ] XCH harness: simultaneous REST + WS consumers with per-`event_id` content comparison (XCH-1/2)
- [ ] SOAK-200 harness (driver + independent consumers + RSS/lag/zero-ERROR assertions) wired to the nightly lane
- [ ] TEN P6 probes: WS auth with foreign-workspace key → close `4404` (WS-3); foreign stats → 404 (permanent)
- [ ] Frontend-consumable WS frame catalog finalized against [../../05-interfaces/api-specification.md](../../05-interfaces/api-specification.md) (Phase 7 input)

## Demo script

1. Bring up a running stream at 10 TPS: phase-05 demo steps 1–4 (seed `4242`).
2. WS tail: `websocat --protocol dataforge.events.v1 ws://localhost:8001/ws/streams/$STREAM/events` — send `{"type":"auth","api_key":"<API key>"}` as the first frame → `ready` frame, then live event frames.
3. Pause: `curl -s -X POST localhost:8000/api/v1/streams/$STREAM/pause -H "Authorization: Bearer $ACCESS"` — WS frames cease within one tick; `GET /api/v1/streams/$STREAM` shows `status: "paused"`; the REST frontier stops advancing.
4. Idempotency: `POST …/pause` again → 200, state unchanged.
5. Resume: `POST …/resume` — WS frames continue; pull the full stream over REST and assert per-shard `sequence_no` contiguity across the pause: `jq -s 'sort_by(.sequence_no) | [.[].sequence_no] | . as $s | [range(1; length)] | map($s[.] - $s[.-1]) | max'` → `1`.
6. In-flight funnels survive: a session mid-checkout before pause produces its `order_placed` after resume (filter the WS capture by `session_id`).
7. Dynamic TPS: `T0=$(date +%s%3N); curl -s -X PATCH localhost:8000/api/v1/streams/$STREAM -H "Authorization: Bearer $ACCESS" -d '{"target_tps":500}'`; sample `GET …/stats` once per second — `observed_tps` reaches ~500 within 2 s of the ack.
8. Stats vs independent tally: run a cursor consumer counting events for 60 s; `GET …/stats` `total_events` delta equals the consumer tally; `last_event_at` within 5 s of now.
9. Negative WS probes: connect without the subprotocol → handshake rejected with HTTP `400`; connect and send no auth frame for 10 s → close `4408`; workspace-B key → close `4404`.
10. WS = REST content: compare the 60 s WS capture against the REST pull over the same window — identical `event_id` sets and per-event content (zero drop notices at this rate).
11. Soak (attended gate run): `make soak` — 200 TPS × 60 min + warmup; assert RSS slope < 1 MiB/min, consumer-lag p99 < 5 s with no positive trend, REST = WS = stats tallies, zero ERROR logs.

## Exit criteria

Binding text with measurable assertions; proving suites per [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §14 (Phase 6 rows).

| # | Binding criterion | Measurable assertion | Proving suite (lane) |
|---|---|---|---|
| 1 | "Pause halts within one tick" | No event with `emitted_at` later than pause-convergence + one tick interval; checkpoint persisted; lease retained | OPS-4 (merge) |
| 2 | "resume continues funnels with zero integrity violations or sequence gaps" | Post-resume stream passes PROP-RI checks; canonical `sequence_no` gapless across pause/resume; stop/restart concatenation byte-identical to an uninterrupted run | OPS-4 + GOLD-D (merge) |
| 3 | "TPS change 10 → 500 takes effect within 2 s" | Observed inter-event rate reaches the new target ≤ 2 s after the PATCH ack | OPS-5 (merge) |
| 4 | "WS and REST deliver the same stream content" | Identical `event_id` sets and per-`event_id` parsed-content equality over a shared window; WS drop-notice counts exactly reconcile any difference | XCH-1/2 (merge) |
| 5 | "1-hour soak at 200 TPS shows stable memory, no consumer-lag growth, and stats matching an independent consumer-side tally" | SOAK-200 thresholds: RSS slope < 1 MiB/min and total growth < 10%; lag slope ≤ 0 with p99 < 5 s; REST tally = WS tally = `total_events`; staleness ≤ 5 s throughout; zero ERROR logs | SOAK-200 (nightly + attended gate run) |
