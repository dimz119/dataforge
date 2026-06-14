"""``manage.py revalidate_builtins_l3`` — the Phase-4 Layer-3 GUARD job.

Re-validates **every** builtin manifest through the real Layer-3 dry run (the same
generic runtime the runner and golden replay use, behavior-engine §1) and FAILS the
process if any builtin does not pass L3 with ``est_eps_per_shard >= 1000``
(MAN-D604, plugin-arch §8.4). This is the Phase-4 CI GUARD that closes the
Phase-3 sequencing window: the only manifests published in the one-phase window
between the L1+L2 validator (Phase 3) and the behaviour engine (Phase 4) are the
builtin subset, which this command retroactively re-validates through L3
(testing-strategy per-phase gates).

It runs the engine dry run directly on each builtin YAML file
(``backend/catalog/builtin/{slug}/{version}.yaml``) — no DB / publish path needed,
so the GUARD is a fast, DB-independent CI step that exercises the real runtime. Each
builtin is parse-hardened + canonicalized (the same front-end the catalog uses) and
then executed under the §8.4 sandbox (fixed seed ``424242424242``, bounded). Any
MAN-D error (including a sub-floor ``est_eps_per_shard``) on any builtin is a
non-zero exit (``CommandError``) — the GUARD fails the release/CI.

Deterministic: the dry run uses the fixed sandbox seed, so the realized metrics are
reproducible; only ``est_eps_per_shard`` (a wall-time throughput measurement) varies
with the worker, which is exactly the floor MAN-D604 gates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from catalog.application import dry_run, ingest
from dataforge_engine.behavior import EPS_FLOOR
from dataforge_engine.manifest import ManifestParseError

# Builtin manifests live under backend/catalog/builtin/{slug}/{version}.yaml.
_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "builtin"


class Command(BaseCommand):
    help = (
        "Re-validate every builtin manifest through the Layer-3 dry run (the real "
        "engine, §8.4) and FAIL if any does not pass with est_eps_per_shard >= 1000 "
        "(MAN-D604). The Phase-4 CI GUARD job."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--builtin-dir",
            default=str(_BUILTIN_DIR),
            help="Override the builtin manifest directory (tests).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        builtin_dir = Path(options["builtin_dir"])
        if not builtin_dir.exists():
            raise CommandError(f"no builtin dir at {builtin_dir}")
        files = sorted(builtin_dir.glob("*/*.yaml"))
        if not files:
            raise CommandError(f"no builtin manifests found under {builtin_dir}")

        failures: list[str] = []
        for path in files:
            failure = self._revalidate_one(path)
            if failure is not None:
                failures.append(failure)

        if failures:
            raise CommandError(
                "Layer-3 GUARD failed for "
                f"{len(failures)}/{len(files)} builtin manifest(s):\n  "
                + "\n  ".join(failures)
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Layer-3 GUARD passed: all {len(files)} builtin manifest(s) "
                f"sustain est_eps_per_shard >= {EPS_FLOOR}."
            )
        )

    def _revalidate_one(self, path: Path) -> str | None:
        """Run L3 on one builtin; return a failure string or ``None`` on pass."""
        text = path.read_text(encoding="utf-8")
        try:
            canonical = ingest.canonicalize(text)
        except ManifestParseError as exc:
            return f"{path}: parse failed ({exc.error.code}): {exc.error.message}"

        result = dry_run.run_layer3_dry_run(canonical.document)
        label = f"{canonical.slug}:{canonical.version}"
        if result.passed:
            self.stdout.write(
                f"{label}: L3 OK (est_eps_per_shard={result.est_eps_per_shard}, "
                f"mean_events_per_session="
                f"{result.metrics.get('mean_events_per_session')})"
            )
            return None

        codes = ", ".join(
            f"{code}({actual}<{bound})" if code == "MAN-D604" else code
            for (code, _path, _msg, bound, actual) in result.errors
        )
        return (
            f"{label}: L3 FAILED [{codes}]; "
            f"est_eps_per_shard={result.est_eps_per_shard} (floor {EPS_FLOOR})"
        )
