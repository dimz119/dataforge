# Exercise E5 — Schema-drift day, then announced upgrade

> **Phase 9 substrate, Phase 10 completion.** Two halves on **one live stream**,
> demonstrating the schema-registry §10.6 distinction the whole phase is built to
> teach: *unannounced* drift versus an *announced* upgrade.
>
> 1. **Drift detection (the chaos half).** The instructor toggles `schema_drift`
>    on. Payloads start carrying the registered **next-version** fields
>    (`shipping_state`) — but `schema_ref.version` still says **v1**. The
>    consumer's job: notice the unknown field, resolve it from the registry diff
>    API, and adapt. (Drift injects only registered next-version fields, ADR-0010
>    / INV-REG-5.)
> 2. **Announced upgrade (the registry half).** The instructor *schedules* the
>    v1→v2 upgrade at a simulated time. The cutover lands between events on
>    `occurred_at ≥ at`; `schema_ref.version` bumps to **v2** with no restart and
>    the stream never stops. The consumer adapts cleanly because it already
>    resolved v2 in half 1.
>
> This is the phase-10 **Demo script** made into a learner lab, runnable against
> `SEED_E2E = 4242`. Exit criteria touched: #1 (live v1→v2 without restart,
> consumers resolve both versions, v1 stragglers keep their `schema_ref`), #3
> (drift fields always resolve to a version above effective), #4 (REG-U* + compat
> surfacing). The answer key for the drift half is per PRD §5 E5: *drift start
> time, affected event types, injected field list, count of v-next events.*

## 0. Prerequisites

* A running stack (`docker compose -f infra/compose/compose.yaml up -d --wait`)
  with the **v2/v3 evolutions registered** (P10-02 seed):
  `python manage.py seed_schema_evolutions` (run after `sync_builtin_scenarios`).
  This registers `ecommerce.order_placed` v2 (`+shipping_state`) and v3
  (`+shipping_city`) via Flow 2.
* An API key with `events:read`, `schemas:read`, `answer_key:read`; a JWT for the
  control-plane upgrade scheduling (`streams:write`). The instructor actions
  (toggle chaos, schedule upgrade) are the JWT/admin surfaces; the consumer
  actions (consume, resolve, diff) are the key surfaces.
* `SEED_E2E = 4242` — deterministic drift sub-seed and a deterministic cutover
  `sequence_no` (phase-10 exit criterion #2).

```bash
API="${API:-http://localhost:8000}"
source infra/scripts/_auth_bootstrap.sh && auth_bootstrap   # ACCESS / WS / KEY

INST=$(curl -fsS -X POST "$API/api/v1/workspaces/$WS/scenario-instances" \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"name":"e5-drift","scenario_slug":"ecommerce","manifest_version":"1.1.0"}' \
  | jq -r '.scenario_instance_id')

# seed 4242 = SEED_E2E. A live stream emitting order_placed v1 (the Phase 8/9 shape).
SID=$(curl -fsS -X POST "$API/api/v1/streams" \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"workspace_id":"'"$WS"'","scenario_instance_id":"'"$INST"'","name":"e5-stream","seed":4242,"target_tps":50}' \
  | jq -r '.stream_id')
curl -fsS -X POST "$API/api/v1/streams/$SID/start" -H "Authorization: Bearer $ACCESS" >/dev/null
```

Confirm the stream's effective version is v1 — the baseline both halves move off:

```bash
curl -fsS "$API/api/v1/streams/$SID/schema-versions" -H "X-API-Key: $KEY" \
  | jq '{effective, pending: [.pending[].subject], applied: [.applied[].subject]}'
#   → {"effective": {"ecommerce.order_placed": 1, ...}, "pending": [], "applied": []}
```

---

## Half 1 — Drift detection (unannounced)

### 1.1 Consumer baseline: a clean v1 sample

Pull a page of `order_placed` and confirm every row is v1 with **no**
`shipping_state` — the schema you think you're consuming:

```bash
curl -fsS "$API/api/v1/streams/$SID/events?types=order_placed&limit=20" -H "X-API-Key: $KEY" \
  | jq '[.data[] | {v: .schema_ref.version, state: .payload.shipping_state}] | unique'
#   → [{"v": 1, "state": null}]     (shipping_state is not a v1 field)
```

### 1.2 Instructor toggles `schema_drift` on

The instructor enables drift via `PATCH /chaos` (api-spec §4.8.3). Drift injects
the **next registered version above effective** — here v2's `shipping_state` —
type-synthesized from the chaos sub-seed, into the payload, while leaving
`schema_ref.version` at v1 (the deliberately confusing part):

```bash
curl -fsS -X PATCH "$API/api/v1/streams/$SID/chaos" -H "Authorization: Bearer $ACCESS" \
  -H 'Content-Type: application/json' \
  -d '{"schema_drift":{"enabled":true,"rate":0.20,"params":{"subjects":["*"],"fields":["*"]}}}' >/dev/null
```

> **CH-V07 — arming requires a registered next version (DR-3).** Drift can only be
> armed if *some* business subject has a registered version above effective. Had
> you skipped the `seed_schema_evolutions` step, this PATCH would be rejected
> `422 manifest-validation-failed` with error code **CH-V07** — there would be no
> next-version field to draw. With v2 registered, arming succeeds. (And once a
> subject reaches its highest registered version, it becomes ineligible again —
> DR-4 menu rebuild.)

### 1.3 Consumer detects the drift

Keep polling. Now some `order_placed` rows carry a `shipping_state` value while
their `schema_ref.version` is still **1** — the field is "from the future":

```bash
curl -fsS "$API/api/v1/streams/$SID/events?types=order_placed&from=latest&limit=200" -H "X-API-Key: $KEY" \
  | jq '[.data[] | select(.payload.shipping_state != null)
                 | {seq: .sequence_no, v: .schema_ref.version, state: .payload.shipping_state}] | .[0:5]'
#   → rows with v:1 BUT state:"CA" etc. — an unknown field under a v1 ref. That is drift.
```

A naive consumer with `additionalProperties: false` against its cached v1 schema
would now **reject** these rows. The lesson: the registry is the source of truth —
go ask it what the field means.

### 1.4 Consumer resolves the drift from the registry diff API

The diff endpoint (api-spec §4.12 #66) names exactly the fields drift may inject
(INV-REG-5) — additions-only under `BACKWARD_ADDITIVE`:

```bash
curl -fsS "$API/api/v1/schemas/ecommerce.order_placed/diff?from=1&to=2" -H "X-API-Key: $KEY"
#   → {"subject":"ecommerce.order_placed","from_version":1,"to_version":2,
#      "added_fields":[{"path":"/properties/shipping_state","type":"string","required":false}],
#      "removed_fields":[],"changed_fields":[]}
```

The consumer now knows `shipping_state` is a known, optional v2 field — it can
relax its schema to v2 and treat the drift rows as forward-compatible. Resolve the
full v2 document to update the parser:

```bash
curl -fsS "$API/api/v1/schemas/ecommerce.order_placed/versions/2" -H "X-API-Key: $KEY" \
  | jq '.schema.properties.shipping_state'
#   → {"type":"string","x-df-binding":{"from":"actor.address.state"}}
```

### 1.5 Grade the drift half against the answer key

The instructor answer key (ADR-0017) exposes the ground truth for `schema_drift`:
start time, affected types, injected field list, and the count of v-next events.
The injection record's `schema_drift` members are `from_version`, `to_version`,
`fields_added[]` (api-spec §4.13):

```bash
# Per-injection detail (one row per drifted event).
curl -fsS "$API/api/v1/streams/$SID/answer-key/injections?mode=schema_drift&limit=5" -H "X-API-Key: $KEY" \
  | jq '.data[] | {event_id, from_version, to_version, fields_added}'

# Aggregate count — the "count of v-next events" the consumer should have flagged.
curl -fsS "$API/api/v1/streams/$SID/answer-key/summary" -H "X-API-Key: $KEY" \
  | jq '{drift_injections: .by_mode.schema_drift.injections, window}'
```

A correct submission flags exactly the `event_id`s the injection log lists, with
`from_version: 1`, `to_version: 2`, `fields_added: ["shipping_state"]` — checkable
to the event. **Note:** drift never touches `cdc.before` images (R-CDC-6), so the
`cdc.users` feed of E4 is unaffected by this half.

---

## Half 2 — Announced upgrade (the registry way)

Now the instructor does it *properly*: a scheduled, announced v1→v2 upgrade. Turn
drift back off first so the two mechanisms don't overlap (the §10.6 distinction):

```bash
curl -fsS -X PATCH "$API/api/v1/streams/$SID/chaos" -H "Authorization: Bearer $ACCESS" \
  -H 'Content-Type: application/json' \
  -d '{"schema_drift":{"enabled":false,"rate":0.20,"params":{}}}' >/dev/null
```

### 2.1 Instructor schedules the upgrade

`at` is **simulated time** (the `occurred_at` domain) so the cutover is
replay-identical at any speed multiplier (event-model §3.5). Pick a virtual time
just ahead of the stream's current virtual clock:

```bash
curl -fsS -X POST "$API/api/v1/streams/$SID/schema-upgrades" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"subject":"ecommerce.order_placed","target_version":2,"at":"2026-06-12T00:00:00.000000Z"}'
#   → 201 {"upgrade_id":"…","subject":"ecommerce.order_placed","target_version":2,
#          "at":"2026-06-12T00:00:00.000000Z","status":"scheduled","created_at":"…"}
```

It now shows as `pending`:

```bash
curl -fsS "$API/api/v1/streams/$SID/schema-versions" -H "X-API-Key: $KEY" \
  | jq '{effective, pending: [.pending[] | {subject, target_version, at, status}]}'
#   → effective still {order_placed:1}, pending: [{…,target_version:2,status:"scheduled"}]
```

> **REG-U001..U007 (exit criterion #4).** The scheduler rejects an invalid upgrade
> `409 conflict` with `errors[] {code, path, message}` listing every failed check:
> a CDC subject (`cdc.users` — REG-U006), a `target_version` not strictly above
> effective (REG-U003), an unregistered version (REG-U002), a second scheduled
> upgrade for the same subject (REG-U007), an `at` before the current virtual time
> (REG-U004). Try one to see the shape:
> ```bash
> curl -fsS -X POST "$API/api/v1/streams/$SID/schema-upgrades" -H "X-API-Key: $KEY" \
>   -H 'Content-Type: application/json' \
>   -d '{"subject":"ecommerce.order_placed","target_version":1}'   # ≤ effective
> #   → 409 conflict, errors:[{"code":"REG-U003","path":"/target_version","message":"…"}]
> ```

### 2.2 Consumer watches the cutover (no restart)

At 50×–60× the simulated midnight arrives quickly. Tail the stream and watch
`schema_ref.version` bump from 1 to 2 mid-stream, with `shipping_state` now a
**bound** value (`actor.address.state`, not synthesized) — and the stream never
stops:

```bash
curl -fsS "$API/api/v1/streams/$SID/events?event_type=order_placed&from=latest&limit=500" -H "X-API-Key: $KEY" \
  | jq -r '.data[] | "\(.sequence_no)\tv\(.schema_ref.version)\t\(.payload.shipping_state // "—")"'
#   48213  v1  —
#   48214  v1  —
#   …
#   49001  v2  CA       ← cutover: first event with occurred_at ≥ at carries v2 + bound field
#   49002  v2  NY
```

The cutover is **atomic between events** on `occurred_at ≥ at`: every event before
the boundary is v1, every event at/after is v2. Same pin + seed + schedule
reproduces the **same cutover `sequence_no`** (exit criterion #2).

### 2.3 The pending → applied transition

After the cutover the upgrade flips to `applied`, with `applied_at_wall` and the
per-shard `applied_sequence_no` (registry §10.3), and effective advances to v2:

```bash
curl -fsS "$API/api/v1/streams/$SID/schema-versions" -H "X-API-Key: $KEY" \
  | jq '{effective, applied: [.applied[] | {subject, target_version, applied_at_wall, applied_sequence_no}]}'
#   → effective {order_placed:2}, applied: [{…,target_version:2,applied_sequence_no:49001}]
```

### 2.4 Consumers resolve both versions; late v1 stragglers keep their ref

A consumer reads both schemas from the registry — the same diff API as half 1
proves the upgrade is purely additive (`shipping_state` green, nothing removed):

```bash
curl -fsS "$API/api/v1/schemas/ecommerce.order_placed/versions/1" -H "X-API-Key: $KEY" | jq '.schema.properties | keys'
curl -fsS "$API/api/v1/schemas/ecommerce.order_placed/versions/2" -H "X-API-Key: $KEY" | jq '.schema.properties | keys'
#   v1 keys: [...8 fields...]      v2 keys: [...8 fields..., "shipping_state"]
```

A **v1 straggler** delivered after the cutover (chaos `late_arriving`, or a cursor
replay) keeps its original `schema_ref.version = 1` and no `shipping_state` — the
upgrade bumps the *generation-time* ref, never rewrites already-canonicalized
events (exit criterion #1). The consumer must key adaptation on the per-event
`schema_ref`, not a global "we upgraded at T" assumption.

## 3. The two-mechanism lesson (schema-registry §10.6)

| | Drift (half 1) | Announced upgrade (half 2) |
|---|---|---|
| `schema_ref.version` | **unchanged** (still v1) | **bumps** to v2 at the cutover |
| Field source | synthesized from chaos sub-seed (post-ledger) | **bound** (`actor.address.state`, in-ledger) |
| Announced? | no — you detect it | yes — scheduled, visible in `pending` |
| Answer key | `schema_drift` injections (start, fields, count) | `applied` upgrade entry (`applied_sequence_no`) |
| Consumer action | detect unknown field, resolve from diff API, relax schema | adapt to the new ref; honor v1 stragglers |
| CDC `before`? | never touched (R-CDC-6) | n/a (no CDC upgrades, REG-U006) |

Both land on **one live stream**, demonstrating that a registry-aware consumer
survives both the messy reality (drift) and the disciplined evolution (upgrade)
without a restart — the Phase 10 thesis.

## 4. Determinism & reproducibility

* **`SEED_E2E = 4242`** fixes the drift sub-seed (which events drift, what
  `shipping_state` values they carry) and the cutover boundary, so the answer-key
  counts and the cutover `sequence_no` reproduce exactly across runs and speeds.
* **`at` is simulated time** — the cutover lands at the same `occurred_at` (and the
  same canonical position) at 1×, 50×, after a pause, after failover, and in
  backfill (registry §10.4). Pausing the stream freezes the virtual clock, so a
  scheduled upgrade *cannot* fire while paused and fires on the first tick after
  resume (P10-07).
* **`manifest_version` is untouched** — only `schema_ref.version` moves; INV-STR-5
  stays intact (the upgrade is a registry-side payload evolution, not a manifest
  change).
