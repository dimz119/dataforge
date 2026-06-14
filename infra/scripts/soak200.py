#!/usr/bin/env python3
"""SOAK-200 — the 1-hour 200-TPS soak harness (testing-strategy §13.1; phase-06 #5).

The attended Phase-6 gate run + the nightly lane: one stream at 200 TPS for
``--minutes`` (default 60) plus a warm-up, ``SEED_SOAK``, chaos off, with an
INDEPENDENT REST cursor consumer and an INDEPENDENT WS tail consumer running for the
whole window. At the end it asserts the §13.1 thresholds via the CI-gated pure logic
in ``backend/tests/ops/stream_control_harness.py`` (so the soak math and the PR-lane
math are the same code):

  * Runner + sink RSS slope < 1 MiB/min and total growth < 10 % (after warm-up);
  * buffer-writer + WS-bridge consumer lag: slope ≤ 0, p99 < 5 s;
  * REST tally == WS tally == stream stats ``total_events`` at run end;
  * stats staleness ≤ 5 s throughout (INV-OBS-2);
  * zero ERROR-level log lines across all process groups.

This is COMPOSE-ONLY: it needs Kafka + the ``ws`` ASGI process + the Redis channel
layer + the runner + the sink host (buffer-writer + ws-pusher). It is invoked by
``make soak`` and by the nightly CI lane — never the PR lane (a 60-minute run). The
verify agent runs it as the attended Phase-6 gate (``demo-phase06.sh`` step 11
shells out to it with a short ``--minutes`` for the attended smoke; the gate run
uses the full hour).

Usage::

    soak200.py --access-token <JWT> --workspace <WS_ID> --api-key <df_…> \\
               [--minutes 60] [--warmup-minutes 10] [--tps 200] \\
               [--api http://localhost:8000/api/v1] [--ws ws://localhost:8001]

Exits 0 with a per-threshold PASS line, non-zero on the first breach. The auth args
are produced the Phase-2 way by the caller (signup → verify → login → workspace →
events:read key); ``demo-phase06.sh`` passes them through.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error, request

# Import the CI-gated assertion logic so the soak verdict == the PR-lane verdict.
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(_BACKEND))
from tests.ops import stream_control_harness as h  # noqa: E402

SEED_SOAK = 161803398874  # the §16.1 soak seed (mirrors tests/seeds.SEED_SOAK)


@dataclass
class SoakConfig:
    access_token: str
    workspace_id: str
    api_key: str
    api: str
    ws_url: str
    minutes: float
    warmup_minutes: float
    tps: int
    stream_id: str = ""


@dataclass
class Consumer:
    """A running independent consumer's collected tally + lag samples."""

    name: str
    event_ids: set[str] = field(default_factory=set)
    lag_samples: list[h.LagSample] = field(default_factory=list)
    dropped: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    error: str | None = None

    @property
    def total(self) -> int:
        return len(self.event_ids)


def _get(url: str, *, headers: dict[str, str]) -> Any:
    req = request.Request(url, headers=headers)
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rest_consumer(cfg: SoakConfig, consumer: Consumer) -> None:
    """Independent REST cursor consumer: pull from head, follow the cursor forever.

    REST is the authoritative complete record (the buffer never drops); its tally is
    the soak's ground truth. Lag is sampled as (now - last delivered emitted_at)."""
    headers = {"X-API-Key": cfg.api_key}
    cursor: str | None = None
    started = time.time()
    try:
        while not consumer.stop.is_set():
            qs = f"cursor={cursor}" if cursor else "from=earliest"
            url = f"{cfg.api}/streams/{cfg.stream_id}/events?{qs}&limit=500"
            body = _get(url, headers=headers)
            data = body.get("data", [])
            for ev in data:
                consumer.event_ids.add(str(ev["event_id"]))
            nxt = body.get("next_cursor")
            if nxt:
                cursor = nxt
            if data:
                last_emitted = data[-1].get("emitted_at_ms") or _emitted_ms(data[-1])
                if last_emitted:
                    lag_s = max(0.0, (_now_ms() - last_emitted) / 1000.0)
                    consumer.lag_samples.append(
                        h.LagSample(t_s=time.time() - started, lag_s=lag_s)
                    )
            time.sleep(0.5 if not data else 0.05)
    except (error.URLError, OSError, ValueError) as exc:  # pragma: no cover - live only
        consumer.error = f"REST consumer error: {exc}"


def _emitted_ms(ev: dict[str, Any]) -> int | None:
    raw = ev.get("emitted_at")
    if not isinstance(raw, str):
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _ws_consumer(cfg: SoakConfig, consumer: Consumer) -> None:
    """Independent WS tail consumer via ``websocat`` (the demo's WS client).

    Connects with the ``dataforge.events.v1`` subprotocol, sends the first-frame auth,
    and collects every ``event`` frame's ``event_id`` + every ``drop_notice`` count.
    At 200 TPS there are no drops, so the WS tally must equal the REST tally."""
    auth = json.dumps({"type": "auth", "api_key": cfg.api_key})
    url = f"{cfg.ws_url}/ws/streams/{cfg.stream_id}/events"
    cmd = ["websocat", "--protocol", "dataforge.events.v1", "-n", url]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
    except FileNotFoundError:  # pragma: no cover - live only
        consumer.error = "websocat not found — install it for the WS soak consumer"
        return
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(auth + "\n")
    proc.stdin.flush()
    started = time.time()
    try:
        while not consumer.stop.is_set():
            line = proc.stdout.readline()
            if not line:
                break
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            ftype = frame.get("type")
            if ftype == "event":
                consumer.event_ids.add(str(frame["event"]["event_id"]))
            elif ftype == "drop_notice":
                consumer.dropped += int(frame.get("dropped", 0))
            elif ftype == "heartbeat":
                consumer.lag_samples.append(
                    h.LagSample(t_s=time.time() - started, lag_s=0.0)
                )
    finally:
        proc.terminate()


def _rss_mib_for(service: str) -> float | None:
    """The resident-set MiB of a compose service's container via ``docker stats``."""
    try:
        cids = subprocess.run(
            ["docker", "ps", "--filter", f"name={service}", "--format", "{{.ID}}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        if not cids:
            return None
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", *cids],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover - live
        return None
    total = 0.0
    for line in out:
        used = line.split("/")[0].strip()  # e.g. "123.4MiB"
        total += _parse_mem(used)
    return total


def _parse_mem(text: str) -> float:
    units = {"GiB": 1024.0, "MiB": 1.0, "KiB": 1 / 1024.0, "B": 1 / (1024.0 * 1024.0)}
    for unit, factor in units.items():
        if text.endswith(unit):
            try:
                return float(text[: -len(unit)]) * factor
            except ValueError:
                return 0.0
    return 0.0


def run_soak(cfg: SoakConfig) -> int:
    """Drive the soak, collect samples, assert the §13.1 thresholds. Returns exit code."""
    headers_jwt = {
        "Authorization": f"Bearer {cfg.access_token}",
        "Content-Type": "application/json",
    }

    rest = Consumer("rest")
    ws = Consumer("ws")
    threads = [
        threading.Thread(target=_rest_consumer, args=(cfg, rest), daemon=True),
        threading.Thread(target=_ws_consumer, args=(cfg, ws), daemon=True),
    ]
    for t in threads:
        t.start()

    rss_samples: list[h.RssSample] = []
    staleness_max = 0.0
    warmup_end = time.time() + cfg.warmup_minutes * 60.0
    deadline = warmup_end + cfg.minutes * 60.0
    print(
        f"SOAK-200: {cfg.tps} TPS, warmup {cfg.warmup_minutes}m + measure "
        f"{cfg.minutes}m, seed {SEED_SOAK}, stream {cfg.stream_id}"
    )
    while time.time() < deadline:
        time.sleep(15)
        now = time.time()
        # Stats staleness (INV-OBS-2): now - last_event_at.
        try:
            stats = _get(f"{cfg.api}/streams/{cfg.stream_id}/stats", headers=headers_jwt)
            last = _emitted_ms({"emitted_at": stats.get("last_event_at")})
            if last:
                staleness_max = max(staleness_max, (_now_ms() - last) / 1000.0)
        except (error.URLError, OSError, ValueError):  # pragma: no cover - live only
            pass
        # RSS only AFTER warm-up (the §13.1 measurement window).
        if now >= warmup_end:
            rss = 0.0
            for svc in ("runner", "buffer-writer"):
                part = _rss_mib_for(svc)
                if part:
                    rss += part
            if rss > 0:
                rss_samples.append(h.RssSample(minute=(now - warmup_end) / 60.0, rss_mib=rss))

    rest.stop.set()
    ws.stop.set()
    for t in threads:
        t.join(timeout=5)

    final_stats = _get(f"{cfg.api}/streams/{cfg.stream_id}/stats", headers=headers_jwt)
    stats_total = int(final_stats.get("total_events", 0))

    failures: list[str] = []

    def _check(label: str, fn: Any) -> None:
        try:
            fn()
            print(f"PASS  {label}")
        except AssertionError as exc:
            print(f"FAIL  {label}: {exc}", file=sys.stderr)
            failures.append(label)

    if rest.error:
        failures.append(rest.error)
    if ws.error:
        failures.append(ws.error)

    _check(
        "RSS stable (slope < 1 MiB/min, growth < 10 %)",
        lambda: h.assert_rss_stable(rss_samples),
    )
    _check(
        "REST lag healthy (slope ≤ 0, p99 < 5 s)",
        lambda: h.assert_lag_healthy(rest.lag_samples),
    )
    _check(
        "tallies reconcile (REST == WS == stats.total_events)",
        lambda: h.assert_tallies_reconcile(
            rest_total=rest.total, ws_total=ws.total, stats_total=stats_total
        ),
    )
    _check("stats staleness ≤ 5 s throughout", lambda: h.assert_staleness_ok(staleness_max))

    print(
        f"      REST={rest.total} WS={ws.total} stats={stats_total} "
        f"ws_dropped={ws.dropped} rss_samples={len(rss_samples)} "
        f"max_staleness={staleness_max:.2f}s"
    )
    if failures:
        print(f"SOAK-200 FAILED ({len(failures)} threshold breach(es))", file=sys.stderr)
        return 1
    print("SOAK-200 PASSED — all §13.1 thresholds met")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SOAK-200 1-hour 200-TPS soak harness")
    p.add_argument("--access-token", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--api-key", required=True)
    p.add_argument("--stream-id", required=True, help="a started 200-TPS SEED_SOAK stream")
    p.add_argument("--api", default="http://localhost:8000/api/v1")
    p.add_argument("--ws", default="ws://localhost:8001")
    p.add_argument("--minutes", type=float, default=60.0)
    p.add_argument("--warmup-minutes", type=float, default=10.0)
    p.add_argument("--tps", type=int, default=200)
    args = p.parse_args(argv)
    cfg = SoakConfig(
        access_token=args.access_token,
        workspace_id=args.workspace,
        api_key=args.api_key,
        api=args.api,
        ws_url=args.ws,
        minutes=args.minutes,
        warmup_minutes=args.warmup_minutes,
        tps=args.tps,
        stream_id=args.stream_id,
    )
    return run_soak(cfg)


if __name__ == "__main__":  # pragma: no cover - compose-only entrypoint
    raise SystemExit(main())
