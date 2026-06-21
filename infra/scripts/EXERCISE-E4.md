# Exercise E4 — SCD2 via CDC (dbt snapshots)

> **Phase 8 substrate, Phase 10 exercise doc.** The `cdc.users` change-feed —
> Debezium-shaped `op`/`before`/`after` images, gapless `source.entity_version`
> per entity (event-model §4.2, R-CDC-5) — is the raw material for a type-2
> slowly-changing-dimension history. This is the PRD §5 E4 lab and the **CDC-8**
> exit criterion of phase-10-schema-evolution.md: *the dbt-snapshot output is
> byte-equal to the table derived from the answer key's ground-truth mutation log.*
>
> The teaching point: a CDC feed with real before/after images and a gapless
> per-entity version lets an analytics engineer build SCD2 the way they would in
> production — and DataForge can grade it **to the row**, because the answer-key
> API exposes the same canonical mutation log the snapshot is supposed to recover.

## 0. Prerequisites

* **dbt** with the DuckDB adapter — `pip install dbt-duckdb` (it is in the backend
  dev group). DuckDB is the warehouse here; the same snapshot SQL ports to
  Snowflake/BigQuery by swapping the adapter and the strategy `updated_at` cast.
* A running stack (`docker compose -f infra/compose/compose.yaml up -d --wait`)
  and an API key carrying **both** `events:read` and `answer_key:read` — the lab
  consumes the delivered CDC feed (learner side) **and** the answer-key canonical
  log (grader side). `answer_key:read` is admin-grantable only (ADR-0017, A-4).
* `SEED_E2E = 4242` — the lab runs against the deterministic seed so the mutation
  log, and therefore the expected SCD2 table, is reproducible byte-for-byte.

```bash
API="${API:-http://localhost:8000}"
source infra/scripts/_auth_bootstrap.sh && auth_bootstrap   # ACCESS / WS / KEY

# A CDC-enabled ecommerce instance: cdc.enabled includes `users`, with the
# background address-mutation driver on (0.5%/actor/day, R-CDC-3 — these are the
# `u` rows your SCD2 must capture).
INST=$(curl -fsS -X POST "$API/api/v1/workspaces/$WS/scenario-instances" \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"name":"e4-scd2","scenario_slug":"ecommerce","manifest_version":"1.1.0"}' \
  | jq -r '.scenario_instance_id')

# seed 4242 = SEED_E2E → reproducible mutation log → byte-exact grading.
SID=$(curl -fsS -X POST "$API/api/v1/streams" \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"workspace_id":"'"$WS"'","scenario_instance_id":"'"$INST"'","name":"e4-stream","seed":4242,"target_tps":50}' \
  | jq -r '.stream_id')
curl -fsS -X POST "$API/api/v1/streams/$SID/start" -H "Authorization: Bearer $ACCESS" >/dev/null
```

## 1. Pull the `cdc.users` feed (the learner's raw input)

Consume the delivered change-feed over the cursor REST API (api-spec §4.9). The
feed is **headed by `op="r"` snapshot rows** (the current pool image at the stream
head, `occurred_at = virtual_epoch`), then the ordered `u`/`d` deltas. Use the
per-entity filter (`entity_type` + `entity_key`, R-CDC-7) to slice one user, or
pull the whole `cdc.users` topic by `types`:

```bash
# Page the whole cdc.users feed into a JSONL the dbt source reads.
: > e4_cdc_users.jsonl
CURSOR=""
while : ; do
  RESP=$(curl -fsS "$API/api/v1/streams/$SID/events?types=cdc.users&limit=1000${CURSOR:+&cursor=$CURSOR}" \
    -H "X-API-Key: $KEY")
  echo "$RESP" | jq -c '.data[]' >> e4_cdc_users.jsonl
  N=$(echo "$RESP" | jq '.data | length')
  CURSOR=$(echo "$RESP" | jq -r '.next_cursor')   # never null on /events (E-1)
  [ "$N" -eq 0 ] && break        # empty poll == caught up to the tail
done
wc -l e4_cdc_users.jsonl
```

Each line is the delivered 20-key envelope; `op` is non-null and `payload` is the
Debezium sub-envelope: `{before, after, op, ts_ms, source}` where
`source.entity_version` is the gapless per-user version and `source.ts_ms` is the
**simulated** change time (≡ `occurred_at` in ms — the SCD2 `valid_from`).

> **Per-entity slice (R-CDC-7).** To watch one user's history:
> `GET /streams/$SID/events?types=cdc.users&entity_type=users&entity_key=usr_a3f81c2e9b4d`
> — identical semantics on REST and WS; the console LiveTail exposes it as the
> entity filter with `c`/`u`/`d`/`r` op chips per row.

## 2. The dbt snapshot recipe

A dbt **snapshot** is exactly the SCD2 primitive: it tracks how rows change over
time and emits `dbt_valid_from`/`dbt_valid_to`. Point its source at the delivered
feed and key the strategy on `source.entity_version` (gapless, authoritative — so
the snapshot never misses or reorders a mutation).

`profiles.yml` (DuckDB target over the JSONL):

```yaml
e4:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: e4.duckdb
      schema: main
```

`models/sources.yml` — read the delivered CDC JSONL as the source table:

```yaml
version: 2
sources:
  - name: dataforge
    schema: main
    tables:
      - name: cdc_users_raw
        meta:
          external_location: "read_json_auto('e4_cdc_users.jsonl', maximum_object_size=10000000)"
```

`models/stg_cdc_users.sql` — flatten the sub-envelope to one row per mutation,
**excluding deletes** (`d` closes a dimension but seeds no new image), with the
gapless version as the ordering key:

```sql
-- The after-image is the dimension row; before-images are redundant with the
-- prior after-image (R-CDC-1), so SCD2 is built from r/c/u after-images only.
SELECT
  payload.after.user_id                       AS user_id,
  payload.source.entity_version               AS entity_version,
  to_timestamp(payload.source.ts_ms / 1000.0) AS valid_from,   -- simulated change time
  payload.after.address.state                 AS address_state,
  payload.after.address.city                  AS address_city,
  payload.after                               AS after_image
FROM {{ source('dataforge', 'cdc_users_raw') }}
WHERE op IN ('r', 'c', 'u')
```

`snapshots/users_snapshot.sql` — the SCD2 snapshot. The `check` strategy on the
`entity_version` column means each new gapless version closes the prior interval:

```sql
{% snapshot users_snapshot %}
{{
  config(
    target_schema='main',
    unique_key='user_id',
    strategy='check',
    check_cols=['entity_version'],
  )
}}
SELECT user_id, entity_version, valid_from, address_state, address_city, after_image
FROM {{ ref('stg_cdc_users') }}
{% endsnapshot %}
```

Run staging then the snapshot. In a live exercise you'd `dbt snapshot` repeatedly
as new CDC rows arrive (the production cadence); for grading against a fixed seed,
one staging build + one snapshot over the full feed is sufficient because the
gapless `entity_version` makes the result order-independent:

```bash
dbt run  --select stg_cdc_users --profiles-dir .
dbt snapshot --profiles-dir .
```

The `users_snapshot` table now carries `dbt_valid_from`/`dbt_valid_to`: type-2
history, one row per (user, address-version) interval.

## 3. The answer key — ground-truth mutation log

DataForge grades E4 against the **canonical** CDC sequence from the ground-truth
ledger — the same mutations, gapless and in `(shard_id, sequence_no)` order, with
`_df` internal-only (SB-4, INV-GEN-7). This is the byte-exact derivation target.
It is served by the answer-key API, gated on `answer_key:read` (ADR-0017):

```bash
# GET /streams/{id}/answer-key/canonical?types=cdc.users — the clean ledger slice.
: > e4_answer_canonical.jsonl
CURSOR=""
while : ; do
  RESP=$(curl -fsS "$API/api/v1/streams/$SID/answer-key/canonical?types=cdc.users&limit=500${CURSOR:+&cursor=$CURSOR}" \
    -H "X-API-Key: $KEY")
  echo "$RESP" | jq -c '.data[]' >> e4_answer_canonical.jsonl
  CURSOR=$(echo "$RESP" | jq -r '.next_cursor')   # collection cursor: null == last page
  [ "$CURSOR" = null ] && break
done
```

Derive the expected SCD2 table from the canonical log with the identical SCD2
window (lead over the gapless `entity_version`, the construction event-model §4.2
R-CDC-5 guarantees recovers the exact validity intervals):

```sql
-- duckdb e4_expected.duckdb
CREATE OR REPLACE TABLE expected_scd2 AS
WITH m AS (
  SELECT payload.after.user_id            AS user_id,
         payload.source.entity_version    AS entity_version,
         to_timestamp(payload.source.ts_ms / 1000.0) AS valid_from,
         payload.after.address.state      AS address_state,
         payload.after.address.city       AS address_city
  FROM read_json_auto('e4_answer_canonical.jsonl', maximum_object_size=10000000)
  WHERE op IN ('r','c','u')
)
SELECT user_id, entity_version, valid_from,
       lead(valid_from) OVER (PARTITION BY user_id ORDER BY entity_version) AS valid_to,
       address_state, address_city
FROM m;
```

## 4. Grade: byte-equal diff (CDC-8)

The exit criterion is **byte-equality** between the dbt snapshot's SCD2 intervals
and the answer-key-derived table. Project both to the comparable columns (the
snapshot's `dbt_valid_from`/`dbt_valid_to` are the intervals; the open interval's
`valid_to` is `NULL` on both sides), then diff:

```bash
duckdb -c "
  ATTACH 'e4.duckdb'          AS snap (READ_ONLY);
  ATTACH 'e4_expected.duckdb' AS exp  (READ_ONLY);

  WITH got AS (
    SELECT user_id, entity_version, valid_from, dbt_valid_to AS valid_to,
           address_state, address_city
    FROM snap.main.users_snapshot
  )
  SELECT
    (SELECT count(*) FROM got)                                AS snapshot_rows,
    (SELECT count(*) FROM exp.main.expected_scd2)             AS expected_rows,
    (SELECT count(*) FROM (SELECT * FROM got
       EXCEPT SELECT * FROM exp.main.expected_scd2))          AS in_snapshot_not_expected,
    (SELECT count(*) FROM (SELECT * FROM exp.main.expected_scd2
       EXCEPT SELECT * FROM got))                             AS in_expected_not_snapshot;
"
#   → snapshot_rows == expected_rows, and BOTH symmetric-difference counts == 0
#     ⇒ byte-equal SCD2. That is CDC-8 PASS.
```

A non-zero symmetric difference means the snapshot missed a mutation, closed an
interval at the wrong boundary, or admitted a `before`/`d` image it should have
dropped — every divergence is attributable to one gapless `entity_version`, which
is why the grade is exact rather than statistical.

## 5. Why it grades exactly

* **Gapless per-entity version (R-CDC-5).** `source.entity_version` increments by
  exactly 1 per mutation, valid across shards; the SCD2 `lead()` window over it
  recovers the validity intervals with no ambiguity. A version gap would be a
  CDC-1/CDC-2 round-trip bug, not a learner error.
* **Simulated change time as `valid_from`.** `source.ts_ms ≡ occurred_at` (event
  time, never moved by chaos) is the dimension's validity clock; `ts_ms`
  (processing time ≡ `emitted_at`) is deliberately *not* used — teaching the
  event-time/processing-time split that E2 also exercises.
* **Background mutations are CDC-only (R-CDC-3).** The 0.5%/actor/day address
  drift emits `u` rows with `causation_id = null` — exactly the SCD2 deltas; no
  business event is required to see a dimension change.
* **Determinism (SEED_E2E = 4242).** Same seed → same mutation log → same
  expected table; the snapshot is reproducible byte-for-byte, so CDC-8 is a
  nightly + gate-run assertion, not a flaky comparison.

## 6. Where this goes next

* **E5** (the companion lab) layers *schema evolution* on top of this feed: the
  CDC `after` image gains `shipping_state`/`shipping_city` as the registry
  evolves — SCD2 over an evolving dimension.
* **Snowflake/BigQuery.** Swap the dbt adapter and the `to_timestamp` cast; the
  snapshot strategy and the answer-key derivation are warehouse-agnostic — the
  feed is the same delivered envelope on every channel.
