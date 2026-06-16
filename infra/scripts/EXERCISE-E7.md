# Exercise E7 (v2) — Load a DataForge dataset into DuckDB

> **Phase 4 first learner value, extended in Phase 8.** A backfill dataset of
> referentially valid, deterministic events loads straight into DuckDB and supports
> analytics — no streaming infrastructure required. This is the OPS-11 exercise
> (testing-strategy §11) and Phase-4 exit criterion #3: *a 100k-event dataset loads
> into DuckDB per a documented exercise — 100,000 rows, 100 % orders→users join
> match, daily-revenue query returns rows.*
>
> **Phase 8 (v2)** registers the full 8-entity `ecommerce` manifest (`1.1.0`:
> Users, Products, Orders, Payments, Refunds, Inventory, Reviews, Shipments;
> ~20 event types), so the dataset now carries the full **view→cart→checkout→
> purchase→ship→deliver→review** funnel, **diurnal/weekly intensity curves**, and a
> first-class **`cdc.*` change-feed**. The sections below add: a richer funnel
> staging layer (§6), the CDC change-feed / SCD2 view (§7), and a diurnal-shape
> sanity check (§8). v1 §1–§5 still pass unchanged — they prove the round-trip and
> referential integrity end to end.

## 0. Prerequisites

* DuckDB — either the [CLI](https://duckdb.org/docs/installation/) (`duckdb`) or
  the Python package (`pip install duckdb` / it is in the backend dev group).
* A DataForge dataset JSONL. Generate one against a running stack (the demo does
  this for you), or via the API:

```bash
# Create a scenario instance, then a 100k-event dataset (async), poll, download.
# (See infra/scripts/demo-phase04.sh for the full auth + create + poll + download.)
curl -s -X POST localhost:8000/api/v1/datasets \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"workspace_id":"'$WS'","scenario_instance_id":"'$INST'",
       "name":"e7","seed":"271828182845","simulated_days":30,"compression":"none"}'
# Phase 8: pin the instance to ecommerce 1.1.0 for the full funnel + CDC feed.
# 30 simulated days surface the weekly curve; the file is HEADED by the CDC
# snapshot (op="r") rows that seed the change-feed in §7 (api-spec §4.10).
# ... poll GET /api/v1/datasets/$DATASET_ID?workspace_id=$WS until status=ready ...
curl -s "localhost:8000/api/v1/datasets/$DATASET_ID/download?workspace_id=$WS" -o e7.jsonl
```

The download is **delivered-shape** JSONL: one JSON object per line, the 20-key
envelope, `_df` stripped (INV-DEL-2). Every business event carries an `actor_id`
(the acting user), an `occurred_at` (simulated time), and a nested `payload`.

## 1. Load the JSONL into DuckDB

```sql
-- duckdb e7.db
CREATE OR REPLACE TABLE events AS
  SELECT * FROM read_json_auto('e7.jsonl', maximum_object_size=10000000);
```

`read_json_auto` infers the schema, including the nested `payload` struct and
`schema_ref` struct. (For a `.jsonl.gz` download, DuckDB reads gzip directly, or
gunzip first to keep this command verbatim.)

## 2. Build the staging views

The events stream is the substrate; these three views are the analytics surface a
dbt `staging/` layer would materialize.

```sql
-- The user universe: every distinct acting user observed in the stream.
CREATE OR REPLACE VIEW users AS
  SELECT DISTINCT actor_id AS user_id FROM events WHERE actor_id IS NOT NULL;

-- One row per placed order, with its owning user.
CREATE OR REPLACE VIEW orders AS
  SELECT payload.order_id AS order_id, payload.user_id AS user_id, occurred_at
  FROM events WHERE event_type = 'order_placed';

-- One row per authorized payment, with its amount.
CREATE OR REPLACE VIEW payments AS
  SELECT payload.order_id AS order_id,
         CAST(payload.amount AS DECIMAL(18,2)) AS amount, occurred_at
  FROM events WHERE event_type = 'payment_authorized';
```

## 3. The three E7 assertions

### E7.1 — Row count

```sql
SELECT count(*) AS rows FROM events;          -- expect 100000 for the demo dataset
```

### E7.2 — orders→users FK join match = 100 %

Every placed order references a user that actually exists in the stream — the
referential integrity the engine guarantees structurally (INV-GEN-1) survives the
round-trip to an analytics engine.

```sql
SELECT
  count(*) AS orders,
  count(*) FILTER (WHERE u.user_id IS NOT NULL) AS matched,
  100.0 * count(*) FILTER (WHERE u.user_id IS NOT NULL) / count(*) AS match_pct
FROM orders o
LEFT JOIN users u USING (user_id);             -- expect match_pct = 100.0
```

### E7.3 — Daily revenue returns rows

```sql
SELECT CAST(occurred_at AS DATE) AS day,
       count(*) AS orders,
       sum(amount) AS revenue
FROM payments
GROUP BY 1 ORDER BY 1;                          -- expect ≥ 1 row
```

## 4. One-command assertion (CI / self-check)

The exact same three checks, automated — used by `demo-phase04.sh` step 8 and the
OPS-11 merge-lane test:

```bash
infra/scripts/e7_duckdb_assert.py e7.jsonl --expect-rows 100000
# PASS  E7.1 row count: 100000 events loaded
# PASS  E7.2 orders→users FK join: 2036/2036 (100%)
# PASS  E7.3 daily revenue: 3 day(s), total revenue 775397.79 across 1923 payments
# OK    all three E7 assertions passed
```

The script exits non-zero on the first failed assertion.

## 6. The full funnel (Phase 8, manifest 1.1.0)

With the 8-entity manifest, the stream carries the whole lifecycle. These views are
the dbt `staging/` layer for the funnel; each maps one event type to one fact table.

```sql
CREATE OR REPLACE VIEW sessions AS
  SELECT session_id, actor_id AS user_id, occurred_at
  FROM events WHERE event_type = 'session_started';

CREATE OR REPLACE VIEW product_views AS
  SELECT session_id, payload.product_id AS product_id, occurred_at
  FROM events WHERE event_type = 'product_viewed';

CREATE OR REPLACE VIEW shipments AS
  SELECT payload.shipment_id AS shipment_id, payload.order_id AS order_id,
         event_type AS status, occurred_at
  FROM events WHERE event_type IN ('shipment_dispatched','shipment_delivered','shipment_lost');

CREATE OR REPLACE VIEW refunds AS
  SELECT payload.refund_id AS refund_id, payload.order_id AS order_id, occurred_at
  FROM events WHERE event_type = 'refund_issued';
```

**Funnel conversion** (the PRD §4.3 target: order-per-session ∈ [14 %, 19 %]):

```sql
SELECT
  (SELECT count(*) FROM sessions)                         AS sessions,
  (SELECT count(*) FROM orders)                           AS orders,
  100.0 * (SELECT count(*) FROM orders)
        / (SELECT count(*) FROM sessions)                 AS order_per_session_pct;
```

**Structural referential integrity over the full funnel** — every refund has a
delivered-or-lost shipment, no payment without an order (PROP-RI; survives the
round-trip):

```sql
SELECT count(*) AS orphan_refunds FROM refunds r
LEFT JOIN shipments s
  ON s.order_id = r.order_id AND s.status IN ('shipment_delivered','shipment_lost')
WHERE s.shipment_id IS NULL;                    -- expect 0
```

## 7. The CDC change-feed → SCD2 (exercise E4 substrate)

Phase 8 emits a first-class `cdc.{entity}` change-feed: each row carries `op`
(`c`/`u`/`d`/`r`) and Debezium-shaped `before`/`after` images. The dataset is
**headed by the `r` snapshot rows** (current images at the stream head), then
ordered `u`/`d` deltas — the gapless `source.entity_version` per entity is the
authoritative per-row order (event-model §4.2). Build an SCD2 history from it:

```sql
CREATE OR REPLACE VIEW cdc_users AS
  SELECT payload.after.user_id AS user_id,
         op,
         payload.source.version AS entity_version,
         occurred_at AS valid_from,
         payload.after AS after_image
  FROM events WHERE event_type = 'cdc.users';

-- SCD2: each row is valid until the next version of the same user.
CREATE OR REPLACE VIEW users_scd2 AS
  SELECT user_id, entity_version, valid_from, after_image,
         lead(valid_from) OVER (PARTITION BY user_id ORDER BY entity_version) AS valid_to
  FROM cdc_users WHERE op IN ('r','c','u');

-- No version gaps and no u/d before the c/r seed (CDC-1/CDC-2 round-trip check):
SELECT count(*) AS bad_chains FROM (
  SELECT user_id,
         entity_version - lag(entity_version)
           OVER (PARTITION BY user_id ORDER BY entity_version) AS gap
  FROM cdc_users
) WHERE gap IS NOT NULL AND gap <> 1;            -- expect 0
```

> **Per-entity CDC filtering (R-CDC-7).** On the *live* channels you can pull one
> entity's change-feed slice directly — the per-entity filter is a query capability
> with **identical semantics on REST and WS** (Phase 8): `GET
> /streams/{id}/events?types=cdc.users&entity_type=users&entity_key=usr_a3f81c2e9b4d`,
> or the same `types` + `entity_type`/`entity_key` in the WS `auth` frame. The
> console's LiveTail exposes it as the **entity filter** next to the event-type
> filter, with `c`/`u`/`d`/`r` op chips per row.

## 8. Diurnal / weekly shape (STAT-SHAPE)

The 30-day backfill realizes the intensity curves (renormalized to mean 1.0, so the
*shape* changes but the daily average TPS does not). The hour-of-day histogram shows
the diurnal peak-to-trough; a flat histogram would mean curves were not applied:

```sql
SELECT extract(hour FROM occurred_at) AS hour_utc, count(*) AS events
FROM events WHERE event_type = 'session_started'
GROUP BY 1 ORDER BY 1;          -- expect a peaked profile, not uniform
```

## 9. Where this goes next

* **Phase 9** adds chaos (duplicates, late/out-of-order, nulls, corruption) — the
  same analytics now exercises late-arriving and de-duplication handling.
* **Phase 5+** streams the same canonical events live (Kafka/WS/REST); the
  analytics here is identical whether the source is a batch download or a stream
  cursor — same envelope, same referential guarantees, same `cdc.*` feed.
