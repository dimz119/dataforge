# Exercise E7 (v1) — Load a DataForge dataset into DuckDB

> **Phase 4 first learner value.** A backfill dataset of referentially valid,
> deterministic events loads straight into DuckDB and supports analytics — no
> streaming infrastructure required. This is the OPS-11 exercise (testing-strategy
> §11) and Phase-4 exit criterion #3: *a 100k-event dataset loads into DuckDB per a
> documented exercise — 100,000 rows, 100 % orders→users join match, daily-revenue
> query returns rows.*
>
> The full diurnal/funnel realism (and a richer dbt staging layer) arrives in
> Phase 8; E7 v1 proves the round-trip and referential integrity end to end.

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
       "name":"e7","seed":"271828182845","simulated_days":7,"compression":"none"}'
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

## 5. Where this goes next

* **Phase 8** adds the full 8-entity manifest, diurnal/weekly shape, and CDC
  feeds — the dataset gains realistic daily curves and a `cdc.*` change-feed you
  can build SCD2 snapshots from (exercise E4).
* **Phase 5+** streams the same canonical events live (Kafka/WS/REST); the
  analytics here is identical whether the source is a batch download or a stream
  cursor — same envelope, same referential guarantees.
