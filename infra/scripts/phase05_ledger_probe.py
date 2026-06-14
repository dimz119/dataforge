#!/usr/bin/env python3
"""Phase-5 demo ledger probe — run INSIDE a backend container (Django context).

The kill-test (demo-phase05.sh step 9) needs to read the canonical ground-truth
ledger for one stream and assert the §8.5 invariant: gapless ``sequence_no``, zero
duplicates, and emission resumed past the pre-kill high-water. This script does
exactly that, reusing the *unit-gated* harness logic
(``tests.ops.failover_harness.scan_ledger_sequence`` / ``assert_canonical_failover``)
so the script and CI agree on the pass/fail definition.

It must run where Django + Postgres are reachable (the api/worker container):

    docker compose exec -T worker python /app/../infra/scripts/phase05_ledger_probe.py \
        --stream "$STREAM" [--mode snapshot|assert] [--pre-kill-last N]

Modes:
  * ``snapshot`` — print the current high-water ``sequence_no`` (the pre-kill mark);
    exits 0, prints just the integer (or ``0`` if none yet).
  * ``assert``   — read every ledger row for the stream, scan for gaps/dups, and
    assert resume past ``--pre-kill-last``; prints a PASS/FAIL line and exits
    non-zero on violation.

The script adds the backend package to ``sys.path`` and bootstraps Django; in the
container the working dir is ``/app`` (the backend root), so the harness import
resolves. When invoked from the host it expects the same layout under ``backend/``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _bootstrap_django() -> None:
    here = Path(__file__).resolve()
    # Container: /app is the backend root (compose mounts ../../backend:/app).
    # Host: backend/ is a sibling two levels up from infra/scripts/.
    candidates = [Path("/app"), here.parents[2] / "backend"]
    for root in candidates:
        if (root / "manage.py").exists():
            sys.path.insert(0, str(root))
            break
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    import django

    django.setup()


def _ledger_sequence_nos(stream_id: str, shard_id: int = 0) -> list[int]:
    from generation.domain.models import GroundTruthLedger

    return list(
        GroundTruthLedger.objects.filter(stream_id=stream_id, shard_id=shard_id)
        .order_by("sequence_no")
        .values_list("sequence_no", flat=True)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-5 demo ledger probe.")
    parser.add_argument("--stream", required=True)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--mode", choices=("snapshot", "assert"), default="snapshot")
    parser.add_argument("--pre-kill-last", type=int, default=-1)
    args = parser.parse_args(argv)

    _bootstrap_django()
    from tests.ops.failover_harness import assert_canonical_failover, scan_ledger_sequence

    seqs = _ledger_sequence_nos(args.stream, args.shard)
    report = scan_ledger_sequence(seqs)

    if args.mode == "snapshot":
        print(report.last_seq if report.last_seq is not None else 0)
        return 0

    # mode == assert
    try:
        assert_canonical_failover(report, pre_kill_last_seq=args.pre_kill_last)
    except AssertionError as exc:
        print(f"FAIL canonical ledger: {exc}")
        return 1
    print(
        f"PASS canonical ledger: {report.count} rows, seq "
        f"[{report.first_seq}..{report.last_seq}], gapless, no dups, "
        f"resumed past {args.pre_kill_last}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
