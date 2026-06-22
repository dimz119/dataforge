# DataForge — Measured-Ceiling Report (LOAD-5K, P11-14)

> **Status: TEMPLATE / reduced-scale local run.**
> The measured numbers in §3 below come from a **reduced-scale local k6 run** on the
> dev compose stack — they prove the harness, the PROP-RI integrity sampler, and the
> TEN cross-tenant probes all work end to end. **They are NOT the GA 5,000-TPS claim.**
> The GA gate (≥ 5,000 aggregate TPS for 30 min, exit criterion #1) is a **prod gate
> run that is SKIPPED per the Phase-11 scope decision** (no live Fly deploy). When the
> prod gate is run, replace §3's "reduced-scale local" rows with the gate numbers and
> drop this banner. This report is cited by
> [`../../specs/02-architecture/scaling-strategy.md`](../../specs/02-architecture/scaling-strategy.md) §3 (the TPS staircase) as the source of the
> measured-vs-planning reconciliation.

---

## 1. What was run

| Item | Value |
|---|---|
| Harness | `infra/loadtest/load-5k.js` (k6) |
| Scenarios | `cursor_pollers` (REST cursor paging), `ws_tails` (~50 WS tails), `control_churn` (lifecycle) |
| Integrity sampler | `infra/loadtest/integrity_sampler.py` (PROP-RI reservoir, ~1% of delivered events) |
| TEN spot probes | `infra/loadtest/ten_spot_probes.py` (cross-tenant masking under load) |
| Thresholds (exit #1) | events p95 < 500 ms · error rate < 0.1% · zero 5xx |

**Reduced-scale local config (what the verify phase ran):** _fill in actual `-e` flags_
e.g. `WORKSPACES=2  STREAMS_PER_WS=2  TPS=80  WS_TAILS=20  CHURN_VUS=2  DURATION=2m`
→ aggregate target ≈ `WORKSPACES × STREAMS_PER_WS × TPS` = **_____ TPS** (local).

**Documented GA gate config (NOT run — prod gate, skipped per scope):**
`WORKSPACES=10  STREAMS_PER_WS=5  TPS=100  WS_TAILS=50  CHURN_VUS=4  VUS=40  DURATION=30m`
→ aggregate target = 10 × 5 × 100 = **5,000 TPS** sustained for 30 min.

---

## 2. The TPS staircase — planning numbers (from scaling-strategy.md §3)

These are the **planning** rungs and their named bottleneck + remedy. The Phase-11
exit criterion lives between rungs 4 and 5 (the GA load-test floor is the 1,000-TPS
neighborhood proving 5× generation/Kafka headroom). Per-shard generation planning
ceiling is **2,500 events/s** (§2.1); admission caps provisioned ΣTPS at **70%** of
measured capacity (3,500 at GA → admission budget ≈ 2,450 EPS, §5).

| Rung | Aggregate TPS | Shards | Runner machines | Kafka | Buffer writers | Buffer store | Named bottleneck | Remedy |
|---|---|---|---|---|---|---|---|---|
| §3.1 | 1 | 1 | 2 (GA baseline) | single broker, 12 partitions | 1 (co-located) | Postgres | none structural — first-event latency | keep baseline; start→first-event < 5 s |
| §3.2 | 10 | 1 | 2 | 〃 | 1 | Postgres | none — deploy churn is the only risk | graceful drain + lease failover (designed) |
| §3.3 | 100 | 1 | 2 | 〃 | 1 | Postgres (~26 GB) | WS cohort fan-out | per-conn cap + sampling + type filters |
| §3.4 | 1,000 | 1–2 | 2 | 〃 | 1 | Postgres (~530 GB) | **storage churn** | ledger tiering, volume → 1 TB, PgBouncer |
| §3.5 | 10,000 | 4 | 3 (+ sink group) | **managed**, 48 partitions | 2–4 | **ClickHouse** | **Postgres buffer ingest + storage** | execute §2.5 off-ramp; managed Kafka forced by >5k trigger |
| §3.6 | 100,000 | 40 | 5–6 × performance-8x (+ sink) | managed cluster 3–6 brokers, 192 partitions | 8 | ClickHouse ×2–3 | **generation CPU economics + egress cost** | native hot path (2–3×), horizontal scale, egress controls |

**Sharding note (binding):** Kafka partition count stays **12 at GA** and is sized for
*aggregate* TPS, **not grown per shard** — isolation is by `partition_key`; partition
growth only happens via a new topic generation (`…events.v2`) at the managed-migration
boundary (§2.3). A multi-shard stream does **not** get dedicated partitions per shard.

---

## 3. Measured — reduced-scale local run (NOT the GA claim)

> Fill these from the verify-phase k6 summary + the two python samplers. Every cell
> labelled "local" is a dev-compose measurement, explicitly not the GA 5k number.

### 3.1 Throughput & latency (k6 summary)

| Metric | Threshold (exit #1) | Measured (reduced-scale local) | Pass? |
|---|---|---|---|
| Aggregate target TPS | — | _____ (local) | — |
| Realized REST events polled | — | `df_events_polled` = _____ | — |
| WS events consumed | — | `df_ws_events` = _____ | — |
| WS frames dropped (drop_notice) | accurate counts (INV-DEL-5) | `df_ws_drops` = _____ | — |
| `cursor_pollers` p95 latency | < 500 ms | _____ ms | ☐ |
| Error rate (`df_errors`) | < 0.1% | _____ % | ☐ |
| 5xx count (`df_5xx`) | == 0 | _____ | ☐ |
| WS upgrades (`df_ws_connects`) | ≈ WS_TAILS | _____ | — |
| Control-plane ops (`df_churn_ops`) | no 5xx | _____ | — |

### 3.2 Integrity sampler (PROP-RI, ~1% reservoir)

| Check | Result (local) |
|---|---|
| ENVELOPE-20KEY (delivered field set) | _____ |
| PROP-RI-5 (sequence_no monotone per shard) | _____ |
| PROP-RI-6 (occurred_at monotone per actor) | _____ |
| PROP-RI-2 (payment requires prior order) | _____ |
| SHARD-OWN (partition_key → emitting shard) | _____ (N/A at shard_count=1) |
| **Verdict** | zero integrity violations on the sample → **_____** |

### 3.3 TEN cross-tenant spot probes (under load)

| Probe (foreign credential) | Expected | Result (local) |
|---|---|---|
| GET A's stream detail | 404 (never 403/2xx/5xx) | _____ |
| GET A's `/events` (data plane) | 404 | _____ |
| GET A's workspace api-keys | 404 | _____ |
| POST pause A's stream | 404 | _____ |
| no-credential | 401 | _____ |
| A-sentinel leak in any body | none | _____ |
| **Verdict** | zero isolation breaches across all rounds → **_____** |

---

## 4. Per-rung bottleneck arithmetic (the staircase math, with the measured slot)

For each rung below, the arithmetic is the **planning** derivation from §2; the
"measured" column is filled only for the rung the local run actually exercised (and
later, for rung 4–5, by the prod gate). This is the §2 staircase reconciliation
scaling-strategy.md §3 cites.

- **Rung 1 (1 TPS):** generation 1/2,500 = 0.04% of one shard; Kafka ~1.25 KiB/s;
  buffer 1 row/s. Bottleneck: first-event latency (< 5 s). _Measured local: ____._
- **Rung 2 (10 TPS):** still < 0.5% generation; deploy churn is the only risk
  (graceful drain + lease failover). _Measured local: ____._
- **Rung 3 (100 TPS):** WS cohort fan-out is the named bottleneck (60 tails ×
  100 TPS = 6,000 f/s = 20% of one ws machine); per-conn 200 f/s cap + sampling +
  type filters keep it linear in connections. _Measured local: ____._
- **Rung 4 (1,000 TPS):** storage churn — 86.4M rows/day ⇒ ~530 GB steady on PG at
  48 h tiers; remedy = ledger→Parquet tiering, volume→1 TB, PgBouncer. This is the
  GA load-test neighborhood (5× generation/Kafka headroom). _Measured: prod gate (skipped)._
- **Rung 5 (10,000 TPS):** Postgres buffer ingest + storage → execute the §2.5
  off-ramp to ClickHouse; managed Kafka (48 partitions) already forced by the >5k
  trigger. _Planning only._
- **Rung 6 (100,000 TPS):** generation CPU economics + egress cost → native hot path
  (2–3×) + horizontal scale + egress controls. _Planning only (no demo, per non-goals)._

---

## 5. How to reproduce (the reduced-scale local run)

```bash
# 0. Stack up (compose project 'dataforge'): api :8000, ws :8001, mailpit :8025,
#    postgres :5432, redis :6379, kafka, runner, worker, sinks.

# 1. Run the harness (tiny local rung; ~2 min). Tee the console so the manifest is
#    captured for the samplers (the manifest is printed between the BEGIN/END markers).
k6 run -e WORKSPACES=2 -e STREAMS_PER_WS=2 -e TPS=80 -e WS_TAILS=20 \
       -e DURATION=2m infra/loadtest/load-5k.js 2>&1 | tee /tmp/load5k.out

# 2. Extract the manifest the samplers consume.
sed -n '/LOAD5K_MANIFEST_BEGIN/,/LOAD5K_MANIFEST_END/p' /tmp/load5k.out \
  | sed '1d;$d' > /tmp/load5k-manifest.json

# 3. Integrity sampler — PROP-RI over ~1% of delivered events.
python infra/loadtest/integrity_sampler.py --manifest /tmp/load5k-manifest.json

# 4. TEN cross-tenant spot probes (needs >=2 workspaces; we provisioned 2 above).
python infra/loadtest/ten_spot_probes.py --manifest /tmp/load5k-manifest.json --rounds 10
```

If k6 is not installed, the harness still `node --check`s clean; install k6
(`brew install k6`) to execute. The two python samplers are stdlib-only and run from
the repo root with the dev venv (`uv run python ...`) so the engine invariants
(`dataforge_engine.envelope.DELIVERED_FIELD_SET`, `…partitioning.shard_for_key`)
import; outside the venv they fall back to pinned mirrors with a printed note.
