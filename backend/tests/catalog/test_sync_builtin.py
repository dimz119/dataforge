"""``manage.py sync_builtin_scenarios`` (plugin-arch §10.2).

insert new / sha-match no-op / sha-mismatch HARD FAIL. Builtins are global
(NULL-workspace) and publish in the same transaction (R-DER).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.domain.models import STATUS_PUBLISHED, ManifestVersion, Scenario
from registry.domain.models import SchemaVersion, Subject

pytestmark = pytest.mark.django_db

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "catalog" / "builtin"


def _subset_dir(tmp_path: Path) -> Path:
    """A builtin dir holding only the Phase-3 subset ecommerce 1.0.0.

    The real builtin dir now also ships the Phase-8 full manifest 1.1.0; running
    BOTH in one deploy currently aborts on the cdc.users v2 R-DER-4 compat gate
    (documented + xfail-covered in ``test_publish_ecommerce_1_1_0``). These sync
    mechanics tests exercise the single-file path against the subset only.
    """
    sub = tmp_path / "ecommerce"
    sub.mkdir()
    (sub / "1.0.0.yaml").write_text(
        (_BUILTIN_DIR / "ecommerce" / "1.0.0.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return tmp_path


def test_sync_inserts_and_publishes_builtin(tmp_path: Path) -> None:
    call_command("sync_builtin_scenarios", builtin_dir=str(_subset_dir(tmp_path)))
    scenario = Scenario.objects.get(slug="ecommerce")
    assert scenario.workspace_id is None  # global
    assert scenario.visibility == "global"
    version = ManifestVersion.objects.get(scenario=scenario, version="1.0.0")
    assert version.status == STATUS_PUBLISHED
    assert version.builtin is True
    assert Subject.objects.count() == 13
    assert SchemaVersion.objects.count() == 13


def _counts() -> tuple[int, int, int]:
    return (
        Subject.objects.count(),
        SchemaVersion.objects.count(),
        ManifestVersion.objects.count(),
    )


def test_sync_is_idempotent_on_sha_match(tmp_path: Path) -> None:
    subset = _subset_dir(tmp_path)
    call_command("sync_builtin_scenarios", builtin_dir=str(subset))
    before = _counts()
    # Second run: sha matches → no-op.
    call_command("sync_builtin_scenarios", builtin_dir=str(subset))
    assert before == _counts()


def test_sync_hard_fails_on_sha_mismatch(tmp_path: Path) -> None:
    """An edited published version (different sha256) aborts the release (INV-CAT-1)."""
    subset = _subset_dir(tmp_path)
    call_command("sync_builtin_scenarios", builtin_dir=str(subset))
    # Tamper: rewrite ecommerce 1.0.0 in place with a modified body.
    target = subset / "ecommerce" / "1.0.0.yaml"
    target.write_text(
        target.read_text(encoding="utf-8").replace("E-Commerce", "E-Commerce (tampered)"),
        encoding="utf-8",
    )
    with pytest.raises(CommandError, match="immutable"):
        call_command("sync_builtin_scenarios", builtin_dir=str(subset))


def test_sync_no_builtin_dir_is_noop(tmp_path: Path, capsys: Any) -> None:
    call_command("sync_builtin_scenarios", builtin_dir=str(tmp_path / "missing"))
    assert not Scenario.objects.exists()
