"""``manage.py registry <subcommand>`` — the schema-registry control-plane CLI.

A subcommand dispatcher (the Confluent ``schema-registry`` operator surface).
Phase 10 ships the single Flow-2 write path (schema-registry §5.2):

    python manage.py registry register-version <subject> --schema <path> \
        [--check] [--expected-latest N]

There is deliberately **no ``/api/v1`` write endpoint** and no console form (§12):
registering a curated evolution version is a platform-operator action (deploy/SSH
trust class, the same as the builtin-manifest seed command, plugin-arch §10.2). The
command runs as the platform maintenance role so it can INSERT a global
(NULL-workspace) ``schema_versions`` row (database-schema §9.6 Class H write path) —
in the dev compose / Fly release it is invoked under ``MIGRATE_DATABASE_URL`` like
``sync_builtin_scenarios``. The actor is ``system`` (no request principal).

Behaviour (§5.2):

* runs the §4 + §6 ``BACKWARD_ADDITIVE`` gate + the REG-C007 binding validation
  against the subject's scenario's **latest published manifest**;
* rejects ``cdc.*`` (REG-C012), unknown subjects (REG-C011), and
  ``--expected-latest`` mismatch (REG-C008);
* idempotent: a candidate whose fingerprint equals the *latest* version's exits 0
  with no write and no audit (the re-runnable seed);
* ``--check`` is a dry-run — runs §4 + §6, prints the §6.3 report, writes nothing;
* on success registers ``latest+1`` (``derived_from_definition = NULL``) and emits
  ``registry.schema_version.registered``;
* on any failure prints the §6.3 Flow-2 JSON report
  ``{subject, latest_version, compatible:false, errors:[{code,path,message}]}`` and
  exits non-zero (``CommandError``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from registry.application import explicit_registration as flow2
from registry.application import services
from registry.infra.compat import CompatError

_REGISTER_VERSION = "register-version"


class Command(BaseCommand):
    help = (
        "Schema-registry control-plane CLI. Subcommand: register-version "
        "<subject> --schema <path> [--check] [--expected-latest N] (Flow 2, §5.2)."
    )

    def add_arguments(self, parser: Any) -> None:
        sub = parser.add_subparsers(dest="subcommand", required=True)
        rv = sub.add_parser(
            _REGISTER_VERSION,
            help="Register an explicit evolution version of a subject (Flow 2).",
        )
        rv.add_argument("subject", help="The subject, e.g. <scenario>.<event_type>.")
        rv.add_argument(
            "--schema",
            required=True,
            help="Path to the §4-profile JSON Schema document for the next version.",
        )
        rv.add_argument(
            "--check",
            action="store_true",
            help="Dry-run: run §4 + §6 and print the report without writing.",
        )
        rv.add_argument(
            "--expected-latest",
            type=int,
            default=None,
            help="Assert the subject's current latest version (REG-C008 on mismatch).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["subcommand"] == _REGISTER_VERSION:
            self._register_version(options)
            return
        raise CommandError(f"unknown subcommand: {options['subcommand']}")

    def _register_version(self, options: dict[str, Any]) -> None:
        subject = str(options["subject"])
        candidate = _load_schema(Path(options["schema"]))

        context = services.scenario_context_for_subject(subject)
        if context is None:
            # No scenario / no published manifest ⇒ the subject cannot exist
            # (subjects are created only by manifest publication, §5.1) → REG-C011.
            self._fail(
                flow2.Flow2Report(
                    subject,
                    latest_version=None,
                    compatible=False,
                    errors=[
                        CompatError(
                            "REG-C011",
                            "/",
                            "unknown subject; subjects are created by publishing a manifest",
                        )
                    ],
                )
            )

        assert context is not None  # narrowed: _fail raises
        try:
            with transaction.atomic():
                outcome = flow2.register_explicit_version(
                    subject_name=subject,
                    candidate=candidate,
                    scenario_id=context.scenario_id,
                    workspace_id=context.workspace_id,
                    latest_manifest=context.latest_manifest,
                    expected_latest=options["expected_latest"],
                    dry_run=bool(options["check"]),
                    on_registered=_audit_registered,
                )
        except flow2.Flow2Incompatible as exc:
            self._fail(exc.report)
            return  # unreachable (_fail raises) — for the type checker

        self._report_success(outcome, dry_run=bool(options["check"]))

    def _report_success(self, outcome: flow2.Flow2Outcome, *, dry_run: bool) -> None:
        report = outcome.report
        self.stdout.write(json.dumps(report.to_dict(), indent=2))
        if outcome.idempotent:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{report.subject}: already at version {report.latest_version} "
                    "(idempotent no-op)."
                )
            )
        elif dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"{report.subject}: --check passed (no write).")
            )
        else:
            assert outcome.registered is not None
            self.stdout.write(
                self.style.SUCCESS(
                    f"{report.subject}: registered version {outcome.registered.version}."
                )
            )

    def _fail(self, report: flow2.Flow2Report) -> None:
        """Print the §6.3 Flow-2 report and exit non-zero (a CLI rejection)."""
        raise CommandError(json.dumps(report.to_dict(), indent=2))


def _load_schema(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CommandError(f"schema file not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommandError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise CommandError(f"{path}: schema document must be a JSON object")
    return document


def _audit_registered(*, subject: str, version: int, workspace_id: Any) -> None:
    """Emit ``registry.schema_version.registered`` for the Flow-2 write (INV-AUD-2)."""
    from catalog.application import audit

    audit.emit(
        "registry.schema_version.registered",
        actor="system",
        workspace_id=workspace_id,
        target={"type": "schema_version", "id": f"{subject}:{version}", "label": subject},
        metadata={"subject": subject, "version": version, "flow": "explicit"},
    )
