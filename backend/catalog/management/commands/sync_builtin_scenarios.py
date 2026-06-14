"""``manage.py sync_builtin_scenarios`` — register the repo's builtin manifests.

Runs post-migrate in every deploy (alongside ``provision_kafka_topics``,
deployment-architecture §2.2; the dev compose entrypoint and the prod Fly release
command). Builtin manifests ship as files at
``backend/catalog/builtin/{slug}/{version}.yaml`` (plugin-arch §10.2). For each
file (sorted, deterministic order):

| Case | Action |
|---|---|
| ``(slug, version)`` absent | Run the full L1+L2 pipeline (§8); insert as |
|   | ``published`` (derives + registers v1 for every subset subject |
|   | in the same transaction, R-DER). A builtin failing validation |
|   | **aborts the release** (held to the same wall as tenant manifests). |
| Present, ``sha256`` matches | No-op |
| Present, ``sha256`` differs | **Hard failure** — editing a published version |
|   | in place is an INV-CAT-1 violation; the fix is a new version file |

Builtins are **global** (``visibility = global``, ``workspace_id IS NULL``): their
subjects own a NULL workspace and are readable by every workspace (INV-REG-4). The
command runs as the platform maintenance role (the same trust class as the migrate
command, §9.6 Class H write path) — it is included in the DDL-class command set so
it connects via ``MIGRATE_DATABASE_URL`` and can INSERT global rows. The actor is
``system`` (no request principal during a deploy).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.application import ingest, publish
from catalog.domain.models import STATUS_PUBLISHED, ManifestVersion
from dataforge_engine.manifest import ManifestParseError

# Builtin manifests live under backend/catalog/builtin/{slug}/{version}.yaml.
_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "builtin"


class Command(BaseCommand):
    help = (
        "Register the repo's builtin scenario manifests (insert new / sha-match "
        "no-op / sha-mismatch hard-fail). Runs post-migrate in every deploy."
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
            self.stdout.write(self.style.WARNING(f"no builtin dir at {builtin_dir}; nothing to do"))
            return
        files = sorted(builtin_dir.glob("*/*.yaml"))
        if not files:
            self.stdout.write("no builtin manifests found.")
            return
        for path in files:
            self._sync_one(path)

    def _sync_one(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        try:
            canonical = ingest.canonicalize(text)
        except ManifestParseError as exc:
            raise CommandError(
                f"{path}: parse failed ({exc.error.code}): {exc.error.message}"
            ) from exc

        existing = self._find_existing(canonical.slug, canonical.version)
        if existing is not None:
            if existing.manifest_sha256 == canonical.sha256:
                self.stdout.write(f"{canonical.slug}:{canonical.version}: up to date (sha match)")
                return
            raise CommandError(
                f"{path}: {canonical.slug}:{canonical.version} exists with a different sha256 — "
                "published versions are immutable (INV-CAT-1); ship a new version file."
            )

        self._register(path, text)

    def _find_existing(self, slug: str, version: str) -> ManifestVersion | None:
        scenario = ingest.resolve_scenario(slug, workspace_id=None)
        if scenario is None:
            return None
        return ManifestVersion.objects.filter(
            scenario=scenario, version=version, workspace_id__isnull=True
        ).first()

    def _register(self, path: Path, text: str) -> None:
        try:
            with transaction.atomic():
                draft = ingest.create_draft(
                    text,
                    workspace_id=None,
                    is_workspace_visibility=False,
                    builtin=True,
                )
                result = publish.publish_manifest_version(
                    draft, actor="system", workspace_id=None
                )
        except ingest.ManifestRejected as exc:
            codes = ", ".join(e.get("code", "?") for e in exc.report.get("errors", []))
            raise CommandError(
                f"{path}: builtin manifest failed validation ({codes}); the release is aborted."
            ) from exc
        except publish.ManifestSchemaCompatError as exc:
            raise CommandError(f"{path}: {exc} — release aborted.") from exc

        version = result.manifest_version
        assert version.status == STATUS_PUBLISHED
        registered = [r.subject for r in result.registered if r.created]
        self.stdout.write(
            self.style.SUCCESS(
                f"{version.scenario_id}:{version.version}: published; "
                f"registered {len(registered)} subjects."
            )
        )
