# G1 — REST → local Kafka bridge (the E8 MVP lab)

**Connection guide G1**, versioned with the API. Ships with Phase 5
(delivery-channels §1.3; PRD §5 exercise E8). G2–G4 (hosted Kafka, webhooks, S3)
arrive in Phase 12.

DataForge's internal Kafka is server-side infrastructure you never touch
(delivery-channels §1.1, CB-1). MVP consumption is the **cursor-based REST API**.
This guide is the lab that teaches you to bridge that REST stream into **your own**
local Kafka — the same pattern you would run in production to feed a downstream
pipeline. It is deliberately a user exercise, not a platform feature, before
Phase 12 (CB-4).

The companion script `infra/scripts/g1_bridge_smoke.py` is a runnable, minimal
reference implementation of every step below.

---

## 1. Prerequisites

- A running single-node Kafka you control (the dev compose stack's broker is fine;
  on the host its bootstrap is `localhost:19092`, the `HOST` listener — Kafka
  compose service, deployment-architecture §2.2).
- An API key with the **`events:read`** scope (create one under
  `POST /api/v1/workspaces/{id}/api-keys` with `"scopes": ["events:read"]`; the
  plaintext is revealed once — SEC-KEY-4).
- A running stream id (`POST /api/v1/streams` then `.../start`; see
  `infra/scripts/demo-phase05.sh` steps 3-4).
- `python` with `confluent-kafka` **or** the `kcat` CLI. The smoke script uses
  `confluent-kafka` when importable and otherwise shells out to `kcat`.

```bash
export DF_API="http://localhost:8000/api/v1"
export DF_KEY="dfk_live_…"          # your events:read key
export DF_STREAM="…"                # the stream id
export DF_BOOTSTRAP="localhost:19092"
export DF_TOPIC="bridge.${DF_STREAM}"   # your local topic
```

---

## 2. The poll loop (at-least-once end to end)

The cursor is your durable checkpoint. The cardinal rule for at-least-once
delivery across the bridge: **persist `next_cursor` only AFTER the batch is
durably produced to your Kafka** (flush first, then save). If you crash between
producing and saving, you reprocess the last page on restart — at-least-once, with
duplicates licensed on the delivered stream (event-model §6) and absorbed by your
idempotent producer (§3).

```
cursor := read_checkpoint_file()  # empty on first run
loop:
    if cursor is empty:
        resp := GET {DF_API}/streams/{stream}/events?from=earliest&limit=500
    else:
        resp := GET {DF_API}/streams/{stream}/events?cursor={cursor}&limit=500
        # (sending X-API-Key: {key} on every request)

    on 410 cursor-expired:           # see §4
        cursor := resp.body.earliest_cursor ; continue
    on 200:
        produce_all(resp.data)        # keyed by partition_key (§3)
        producer.flush()              # block until every message is acked
        write_checkpoint_file(resp.next_cursor)   # AFTER the flush
        cursor := resp.next_cursor
        if resp.data is empty: sleep(1s)   # caught up to the live frontier
```

Notes that matter:

- `next_cursor` is **never null** — an empty page at the live tail returns the same
  cursor, so you poll it again next tick (RC-2/RC-3). Sleep briefly on an empty
  page to avoid a hot loop.
- Re-fetching the **same** cursor returns a **byte-identical** body within retention
  (INV-DEL-3, XCH-3) — your checkpoint replay is exact.
- The delivered envelope is exactly the **20-key** contract shape; no `_df`-prefixed
  internal fields ever appear (SB-3). Do not depend on internal fields — there are
  none.

---

## 3. Produce keyed by `partition_key`, idempotently

Produce each event to your topic **keyed by its `partition_key`** so per-key order
is preserved across partitions (the same key the platform used internally, S-5).
Configure the producer for idempotence so the duplicates the at-least-once bridge
emits collapse into per-key FIFO without reordering:

```
enable.idempotence = true
acks               = all
```

```python
for env in page["data"]:
    producer.produce(
        topic=DF_TOPIC,
        key=env["partition_key"],          # preserves per-key order
        value=json.dumps(env).encode(),
    )
producer.flush()                           # then checkpoint (§2)
```

With `kcat` the equivalent is `kcat -P -b $DF_BOOTSTRAP -t $DF_TOPIC -K:` feeding
`key:value` lines (idempotence is broker-side default on modern brokers; prefer
the `confluent-kafka` path for explicit `acks=all`).

---

## 4. Handling `410 cursor-expired`

A cursor can age out of the buffer's retention window (24 h Free / 48 h physical)
or land in a dropped partition. The API then returns **410** with a problem body
(INV-DEL-4, OPS-7):

```json
{
  "type": ".../cursor-expired",
  "status": 410,
  "earliest_cursor": "c1.…",
  "retention_hours": 24
}
```

Recovery is one request away: **reset your cursor to `earliest_cursor` from the
body** and continue. You have skipped the events that expired — **document the
gap** (log the old and new positions and the `retention_hours`) so your downstream
knows a retention gap occurred. A `410` is never a silent skip; treat it as an
explicit, logged reset, not an error to crash on.

A `400 cursor-invalid` is different — it means the cursor is malformed or
fingerprint-bound to a different stream/filter set; fix the cursor (it is not an
expiry).

---

## 5. Verify the bridge

Count what you bridged and compare against the source frontier:

```bash
# Messages in your local topic:
kcat -C -b "$DF_BOOTSTRAP" -t "$DF_TOPIC" -e -q | wc -l
```

Because the bridge is at-least-once, your topic count is **≥** the number of
distinct delivered events (duplicates from checkpoint replay are expected and
licensed). De-duplicate downstream on `event_id` only if your use case requires
exactly-once — but note that chaos **duplicate** injections (Phase 9) are
*intentional* same-`event_id` copies you must NOT dedupe away (BW-4); distinguish
replay dupes (identical bytes) from chaos dupes (distinct internal provenance).

In Phase 6 the `GET /streams/{id}/stats` endpoint gives an authoritative
`total_emitted` to reconcile against; until then, reconcile two independent bridge
runs from the same `earliest` cursor — they must produce the same distinct
`event_id` set (replay stability).

---

## Recap

1. `events:read` key + a running stream.
2. Poll with the cursor; **produce, flush, then checkpoint** (at-least-once).
3. Key by `partition_key` with `enable.idempotence=true, acks=all`.
4. On `410`, reset to `earliest_cursor` and log the gap.
5. Verify bridged count vs the source frontier.

This is the whole MVP consumption contract. When you graduate to hosted per-
workspace Kafka (Phase 12, guide G2) the same envelope shape and per-key ordering
hold — only the transport changes.
