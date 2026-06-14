#!/usr/bin/env python3
"""OPS-11 / Exercise E7 — load a DataForge JSONL dataset into DuckDB and assert the
three published E7 queries (testing-strategy §11 OPS-11; phase-04 exit criterion #3).

The dbt/DuckDB learner exercise (E7 v1): a backfill dataset of delivered-shape
events loads into DuckDB and supports analytics. This script is the *exact*
published assertion harness — the same SQL the exercise doc
(``infra/scripts/EXERCISE-E7.md``) walks a learner through:

  1. **Row count** — ``read_json_auto`` over the JSONL yields the expected event
     count (the phase demo uses a 100,000-event dataset).
  2. **orders→users FK join match = 100 %** — every ``order_placed.payload.user_id``
     resolves to a user observed in the stream (the ``users`` staging view is the
     distinct ``actor_id`` universe), proving referential validity survives the
     round-trip to an analytics engine (INV-GEN-1 end-to-end).
  3. **Daily revenue returns rows** — ``sum(payment_authorized.amount)`` grouped by
     ``occurred_at::date`` produces at least one revenue row.

Usage::

    e7_duckdb_assert.py <dataset.jsonl[.gz]> [--expect-rows N] [--db PATH]

Exits ``0`` with a per-check ``PASS`` line on success, non-zero on the first
failed assertion (so the demo + a CI/merge step can gate on it). Plain (no gzip)
JSONL is preferred for ``read_json_auto``; a ``.gz`` input is decompressed to a
temp file first (DuckDB reads gzip too, but the explicit path keeps the published
command identical to the doc).
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import duckdb
except ModuleNotFoundError:  # pragma: no cover - dependency is in the dev group
    print("FAIL: duckdb is not installed (uv sync installs it from the dev group)", file=sys.stderr)
    sys.exit(2)

# The published E7 DDL/queries — kept verbatim in EXERCISE-E7.md (doc ⇄ code parity).
DDL_EVENTS = (
    "CREATE OR REPLACE TABLE events AS "
    "SELECT * FROM read_json_auto('{path}', maximum_object_size=10000000);"
)
DDL_USERS = (
    "CREATE OR REPLACE VIEW users AS "
    "SELECT DISTINCT actor_id AS user_id FROM events WHERE actor_id IS NOT NULL;"
)
DDL_ORDERS = (
    "CREATE OR REPLACE VIEW orders AS "
    "SELECT payload.order_id AS order_id, payload.user_id AS user_id, occurred_at "
    "FROM events WHERE event_type = 'order_placed';"
)
DDL_PAYMENTS = (
    "CREATE OR REPLACE VIEW payments AS "
    "SELECT payload.order_id AS order_id, "
    "CAST(payload.amount AS DECIMAL(18,2)) AS amount, occurred_at "
    "FROM events WHERE event_type = 'payment_authorized';"
)
Q_ROWS = "SELECT count(*) FROM events;"
Q_ORDERS = "SELECT count(*) FROM orders;"
Q_ORDERS_MATCHED = (
    "SELECT count(*) FROM orders o "
    "WHERE EXISTS (SELECT 1 FROM users u WHERE u.user_id = o.user_id);"
)
Q_DAILY_REVENUE = (
    "SELECT CAST(occurred_at AS DATE) AS day, count(*) AS orders, "
    "sum(amount) AS revenue FROM payments GROUP BY 1 ORDER BY 1;"
)


class E7Failure(AssertionError):
    """An E7 assertion failed (row count / FK join / daily revenue)."""


def _materialize_plain_jsonl(path: Path) -> tuple[Path, Path | None]:
    """Return a plain-JSONL path for ``read_json_auto`` (decompress ``.gz`` to temp)."""
    if path.suffix == ".gz":
        tmp = Path(tempfile.mkstemp(suffix=".jsonl")[1])
        with gzip.open(path, "rb") as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return tmp, tmp
    return path, None


def run_e7(jsonl_path: str | Path, *, expect_rows: int | None = None,
           db_path: str | None = None) -> dict[str, Any]:
    """Run the three E7 assertions; raise :class:`E7Failure` on the first miss.

    Returns a metrics dict ``{rows, orders, orders_matched, revenue_days}`` on
    success — usable by the OPS-11 test and the demo evidence lines.
    """
    src = Path(jsonl_path)
    if not src.exists():
        raise E7Failure(f"dataset not found: {src}")
    plain, cleanup = _materialize_plain_jsonl(src)
    con = duckdb.connect(db_path or ":memory:")
    try:
        con.execute(DDL_EVENTS.format(path=plain.as_posix()))
        con.execute(DDL_USERS)
        con.execute(DDL_ORDERS)
        con.execute(DDL_PAYMENTS)

        rows = int(con.execute(Q_ROWS).fetchone()[0])
        print(f"PASS  E7.1 row count: {rows} events loaded")
        if expect_rows is not None and rows != expect_rows:
            raise E7Failure(f"E7.1 row count {rows} != expected {expect_rows}")

        orders = int(con.execute(Q_ORDERS).fetchone()[0])
        matched = int(con.execute(Q_ORDERS_MATCHED).fetchone()[0])
        if orders == 0:
            raise E7Failure("E7.2 no orders in the dataset — cannot assess the FK join")
        if matched != orders:
            raise E7Failure(
                f"E7.2 orders→users FK join only {matched}/{orders} "
                f"({matched / orders * 100:.2f}%); expected 100%"
            )
        print(f"PASS  E7.2 orders→users FK join: {matched}/{orders} (100%)")

        revenue = con.execute(Q_DAILY_REVENUE).fetchall()
        if not revenue:
            raise E7Failure("E7.3 daily-revenue query returned no rows")
        total = sum((r[2] for r in revenue if r[2] is not None), start=0)
        print(
            f"PASS  E7.3 daily revenue: {len(revenue)} day(s), "
            f"total revenue {total} across {sum(int(r[1]) for r in revenue)} payments"
        )
        return {
            "rows": rows,
            "orders": orders,
            "orders_matched": matched,
            "revenue_days": len(revenue),
        }
    finally:
        con.close()
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OPS-11 / Exercise E7 DuckDB assertions")
    parser.add_argument("jsonl", help="path to the dataset JSONL (.jsonl or .jsonl.gz)")
    parser.add_argument("--expect-rows", type=int, default=None,
                        help="assert the exact event count (the demo uses 100000)")
    parser.add_argument("--db", default=None, help="DuckDB file path (default: in-memory)")
    args = parser.parse_args(argv)
    try:
        run_e7(args.jsonl, expect_rows=args.expect_rows, db_path=args.db)
    except E7Failure as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print("OK    all three E7 assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
