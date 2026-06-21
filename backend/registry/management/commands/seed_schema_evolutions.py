"""``manage.py seed_schema_evolutions`` — register the builtin evolution fixtures.

The Phase-10 seed step (schema-registry §5.2 / §9.1 registration timeline): it
discovers the curated evolution fixtures in the repo and registers each one — v2,
then v3, etc. — for its **builtin** (global) scenario through the Flow-2 path, the
same gate + binding validation the ``registry register-version`` command runs. The
fixtures ship in the repo under ``registry/fixtures/schemas/<subject>/v{N}.json``;
the subject is the fixture directory name and the versions register in ``vN`` order
(v2 before v3). The scenario logic lives entirely in the fixture tree (DATA), never
in this command.

It runs after ``sync_builtin_scenarios`` in a deploy (the builtin manifest must be
published first, so the subject exists and the latest manifest is available as the
binding-resolution context). Idempotent + re-runnable: a fixture whose fingerprint
equals the subject's current latest is a no-op (no write, no audit) — running the
seed twice leaves the subject at v3 either way. Runs as the platform maintenance
role (Class H global write path); the actor is ``system``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from registry.application import explicit_registration as flow2
from registry.application import services

# Curated evolutions ship as repo fixtures (project-folder-structure: registry
# fixtures live under registry/fixtures/schemas/{subject}/v{N}.json). The subject
# is the fixture directory name (DATA in the tree, never a Python literal); the
# versions register in vN order (v2 before v3).
_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "schemas"
_VERSION_FILE = re.compile(r"^v(\d+)\.json$")


def _discover_evolutions(fixture_dir: Path) -> list[tuple[str, Path]]:
    """Discover ``<subject>/v{N}.json`` fixtures, subject-grouped, vN-ordered.

    The subject name is the parent directory name; versions are ordered by the
    numeric ``N`` in the ``vN.json`` filename (v2 before v3) so each registers on
    top of the previous. Returns ``(subject, path)`` pairs in registration order.
    """
    discovered: list[tuple[int, str, Path]] = []
    for path in fixture_dir.rglob("v*.json"):
        match = _VERSION_FILE.match(path.name)
        if match is None:
            continue
        discovered.append((int(match.group(1)), path.parent.name, path))
    # Group by subject, then order each subject's fixtures by numeric version.
    discovered.sort(key=lambda item: (item[1], item[0]))
    return [(subject, path) for _, subject, path in discovered]


class Command(BaseCommand):
    help = (
        "Register the builtin schema evolutions discovered under the fixture tree "
        "via Flow 2 (schema-registry §5.2/§9). Idempotent + re-runnable; runs "
        "post-publish."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--fixture-dir",
            default=str(_FIXTURE_DIR),
            help="Override the schema-fixture directory (tests).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        fixture_dir = Path(options["fixture_dir"])
        for subject, path in _discover_evolutions(fixture_dir):
            self._seed_one(subject, path)

    def _seed_one(self, subject: str, path: Path) -> None:
        if not path.exists():
            raise CommandError(f"evolution fixture not found: {path}")
        candidate = json.loads(path.read_text(encoding="utf-8"))

        context = services.scenario_context_for_subject(subject)
        if context is None:
            raise CommandError(
                f"{subject}: scenario or published manifest absent — run "
                "sync_builtin_scenarios first (the subject is created by publication)."
            )

        # Re-runnable: if this fixture's schema already exists as a registered
        # version (the subject has advanced to or past it), skip it. Registering an
        # already-seeded intermediate fixture on top of a newer latest would fail the
        # gate as a regression (§5.2) — fingerprint equality is the no-op signal (§3.2).
        existing = flow2.find_existing_version(
            subject_name=subject,
            candidate=candidate,
            workspace_id=context.workspace_id,
        )
        if existing is not None:
            self.stdout.write(f"{subject}: already at version {existing} (no-op).")
            return

        try:
            with transaction.atomic():
                outcome = flow2.register_explicit_version(
                    subject_name=subject,
                    candidate=candidate,
                    scenario_id=context.scenario_id,
                    workspace_id=context.workspace_id,
                    latest_manifest=context.latest_manifest,
                    on_registered=_audit_registered,
                )
        except flow2.Flow2Incompatible as exc:
            raise CommandError(
                f"{subject}: evolution rejected — {json.dumps(exc.report.to_dict())}"
            ) from exc

        if outcome.idempotent:
            self.stdout.write(
                f"{subject}: already at version {outcome.report.latest_version} (no-op)."
            )
        else:
            assert outcome.registered is not None
            self.stdout.write(
                self.style.SUCCESS(
                    f"{subject}: registered version {outcome.registered.version}."
                )
            )


def _audit_registered(*, subject: str, version: int, workspace_id: Any) -> None:
    from catalog.application import audit

    audit.emit(
        "registry.schema_version.registered",
        actor="system",
        workspace_id=workspace_id,
        target={"type": "schema_version", "id": f"{subject}:{version}", "label": subject},
        metadata={"subject": subject, "version": version, "flow": "explicit"},
    )
