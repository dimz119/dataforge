#!/usr/bin/env python3
"""G1 smoke — the REST → local Kafka bridge reference (delivery-channels §1.3).

A runnable, minimal implementation of the connection-guide G1 poll loop
(`infra/scripts/G1-rest-to-kafka-bridge.md`): pull a stream's delivered events
from the cursor REST API and produce them into a local Kafka topic, keyed by
`partition_key`, idempotently, checkpointing the cursor AFTER each flush
(at-least-once), and handling `410 cursor-expired` by resetting to the
`earliest_cursor` from the problem body.

Producer backend, chosen at runtime:
  * `confluent-kafka` if importable (idempotent producer, acks=all);
  * else the `kcat` CLI (`kcat -P -K:`);
  * else `--dry-run` mode (no Kafka) — verifies the *poll loop, checkpointing,
    and 410 handling* end to end against a live API without a broker. This is the
    form `demo-phase05.sh` runs so the smoke is exercised even when host Kafka
    tooling is absent.

Exit non-zero on any failure; prints a PASS/FAIL line with the bridged count.

Usage:
  g1_bridge_smoke.py --api URL --key KEY --stream ID \
      [--bootstrap localhost:19092] [--topic NAME] \
      [--max-events N] [--dry-run] [--checkpoint FILE]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 500
POLL_SLEEP_S = 0.5


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _get_events(
    api: str, key: str, stream: str, *, params: dict[str, str]
) -> tuple[int, dict[str, Any]]:
    """GET /streams/{id}/events; returns (status_code, json_body)."""
    qs = urllib.parse.urlencode(params)
    url = f"{api.rstrip('/')}/streams/{stream}/events?{qs}"
    req = urllib.request.Request(url, headers={"X-API-Key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # trusted local API
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 410, 400, 404, 403, 401 land here
        body_raw = exc.read().decode("utf-8") if exc.fp else "{}"
        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            body = {"raw": body_raw}
        return exc.code, body


class _Producer:
    """A tiny producer facade over confluent-kafka / kcat / dry-run."""

    def __init__(self, *, bootstrap: str, topic: str, dry_run: bool) -> None:
        self.topic = topic
        self.bootstrap = bootstrap
        self.backend = "dry-run"
        self._ck: Any = None
        self._buf: list[tuple[str, str]] = []
        if dry_run:
            return
        try:
            from confluent_kafka import Producer  # type: ignore[import-not-found]

            self._ck = Producer(
                {
                    "bootstrap.servers": bootstrap,
                    "enable.idempotence": True,  # G1 §3: per-key FIFO under at-least-once
                    "acks": "all",
                    "client.id": "g1-bridge-smoke",
                }
            )
            self.backend = "confluent-kafka"
        except ImportError:
            if shutil.which("kcat"):
                self.backend = "kcat"
            else:
                _eprint("note: neither confluent-kafka nor kcat present — falling back to dry-run")
                self.backend = "dry-run"

    def produce(self, key: str, value: str) -> None:
        if self.backend == "confluent-kafka":
            self._ck.produce(self.topic, key=key.encode(), value=value.encode())
        else:  # kcat or dry-run: buffer key:value lines, flush in one pipe
            self._buf.append((key, value))

    def flush(self) -> None:
        if self.backend == "confluent-kafka":
            self._ck.flush(10)
        elif self.backend == "kcat" and self._buf:
            # kcat -P -K: reads `key:value` lines on stdin (one message per line).
            lines = "".join(f"{k}:{v}\n" for k, v in self._buf)
            subprocess.run(
                ["kcat", "-P", "-b", self.bootstrap, "-t", self.topic, "-K:"],
                input=lines.encode(),
                check=True,
            )
            self._buf.clear()
        else:  # dry-run: messages are accounted but not produced
            self._buf.clear()


def run_bridge(args: argparse.Namespace) -> int:
    checkpoint = Path(args.checkpoint) if args.checkpoint else None
    cursor = checkpoint.read_text().strip() if checkpoint and checkpoint.exists() else ""

    producer = _Producer(bootstrap=args.bootstrap, topic=args.topic, dry_run=args.dry_run)
    print(f"G1 bridge: producer backend = {producer.backend}, topic = {args.topic}")

    bridged = 0
    reset_count = 0
    empty_polls = 0
    deadline = time.monotonic() + args.timeout

    while bridged < args.max_events and time.monotonic() < deadline:
        if cursor:
            params = {"cursor": cursor, "limit": str(DEFAULT_LIMIT)}
        else:
            params = {"from": "earliest", "limit": str(DEFAULT_LIMIT)}

        status, body = _get_events(args.api, args.key, args.stream, params=params)

        if status == 410:  # G1 §4: reset to earliest_cursor, log the gap, continue
            new_cursor = body.get("earliest_cursor", "")
            print(
                f"410 cursor-expired: reset {cursor or '<earliest>'} -> {new_cursor} "
                f"(retention_hours={body.get('retention_hours')}) — GAP logged"
            )
            if not new_cursor:
                _eprint("FAIL: 410 body had no earliest_cursor")
                return 1
            cursor = new_cursor
            reset_count += 1
            continue
        if status != 200:
            _eprint(f"FAIL: GET events returned {status}: {json.dumps(body)[:300]}")
            return 1

        data = body.get("data", [])
        for env in data:
            # G1 §3: key by partition_key, value = the full delivered envelope.
            producer.produce(str(env["partition_key"]), json.dumps(env, sort_keys=True))
        producer.flush()  # durably produced BEFORE we advance the checkpoint
        bridged += len(data)

        next_cursor = body.get("next_cursor")
        if not next_cursor:
            _eprint("FAIL: next_cursor was null (contract violation RC-2/RC-3)")
            return 1
        if checkpoint:  # checkpoint AFTER the flush (at-least-once, G1 §2)
            checkpoint.write_text(next_cursor)
        cursor = next_cursor

        if not data:  # caught up to the live frontier
            empty_polls += 1
            if empty_polls >= args.idle_polls:
                break
            time.sleep(POLL_SLEEP_S)
        else:
            empty_polls = 0

    ok = bridged > 0 or args.allow_empty
    verdict = "PASS" if ok else "FAIL"
    print(
        f"{verdict}: bridged {bridged} event(s) to '{args.topic}' "
        f"via {producer.backend} (cursor resets: {reset_count})"
    )
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="G1 REST → local Kafka bridge smoke.")
    p.add_argument("--api", required=True, help="API base, e.g. http://localhost:8000/api/v1")
    p.add_argument("--key", required=True, help="API key with events:read")
    p.add_argument("--stream", required=True, help="stream id")
    p.add_argument("--bootstrap", default="localhost:19092", help="Kafka bootstrap (HOST listener)")
    p.add_argument("--topic", default=None, help="target topic (default: bridge.<stream>)")
    p.add_argument("--max-events", type=int, default=200, help="stop after N bridged events")
    p.add_argument("--idle-polls", type=int, default=3, help="empty polls before caught-up")
    p.add_argument("--timeout", type=float, default=60.0, help="overall wall-clock budget (s)")
    p.add_argument("--checkpoint", default=None, help="cursor checkpoint file (at-least-once)")
    p.add_argument("--dry-run", action="store_true", help="no Kafka; exercise the poll loop only")
    p.add_argument(
        "--allow-empty",
        action="store_true",
        help="PASS even if zero events were available (frontier already drained)",
    )
    args = p.parse_args(argv)
    if args.topic is None:
        args.topic = f"bridge.{args.stream}"
    return args


def main(argv: list[str] | None = None) -> int:
    return run_bridge(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
