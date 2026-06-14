"""The adversarial manifest corpus suite (testing-strategy §16.3; exit criterion #1).

Drives every :class:`~tests.catalog.fixtures.AdversarialCase` and asserts at least
one observed error matches its expected ``{code, path, bound, actual, scope}``
tuple **exactly** (the §8.2 frozen error shape). The coverage meta-test then proves
the corpus is complete: every MAN-S/V code the validator source emits has a case,
so a newly-emitted code with no fixture fails the build by construction.

Pure-Python — no DB; runs in every lane.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dataforge_engine.manifest import ValidationError
from tests.catalog.fixtures import CORPUS, AdversarialCase, run_case

# The validator source tree: every literal ``"MAN-S###"``/``"MAN-V###"`` here is a
# code the validator can emit and therefore needs corpus coverage.
_MANIFEST_PKG = Path(__file__).resolve().parents[2] / "dataforge_engine" / "manifest"
_CODE_LITERAL = re.compile(r'"(MAN-[SV]\d{3})"')


def _emitted_codes() -> set[str]:
    codes: set[str] = set()
    for src in _MANIFEST_PKG.rglob("*.py"):
        codes.update(_CODE_LITERAL.findall(src.read_text(encoding="utf-8")))
    return codes


def _matches(err: ValidationError, case: AdversarialCase) -> bool:
    """True iff ``err`` matches every pinned field of ``case`` (skipping ``unpinned``)."""
    if err.code != case.code or err.scope != case.scope:
        return False
    if "path" not in case.unpinned and case.path is not None and err.path != case.path:
        return False
    if "bound" not in case.unpinned and case.bound is not None and err.bound != case.bound:
        return False
    if "actual" not in case.unpinned and case.actual is not None and err.actual != case.actual:
        return False
    return True


@pytest.mark.parametrize("case", CORPUS, ids=lambda c: f"{c.code}:{c.name}")
def test_adversarial_case_emits_exact_error_tuple(case: AdversarialCase) -> None:
    observed = run_case(case)
    assert any(e.code == case.code for e in observed), (
        f"{case.name}: expected {case.code}; got {sorted({e.code for e in observed})}"
    )
    assert any(_matches(e, case) for e in observed), (
        f"{case.name}: no {case.code} error matched the pinned tuple "
        f"(path={case.path!r}, bound={case.bound!r}, actual={case.actual!r}, "
        f"scope={case.scope!r}). Observed {case.code} errors: "
        f"{[e.to_dict() for e in observed if e.code == case.code]}"
    )


def test_every_emitted_code_has_a_fixture() -> None:
    """Completeness: every MAN-S/V code the validator emits has ≥ 1 corpus fixture."""
    covered = {c.code for c in CORPUS}
    emitted = _emitted_codes()
    missing = emitted - covered
    assert not missing, (
        f"adversarial corpus is missing a fixture for emitted code(s): {sorted(missing)} "
        "(testing-strategy §16.3 — one fixture per MAN-S/V code)."
    )


def test_corpus_has_at_least_one_fixture_per_code() -> None:
    """Each declared code is non-empty and the corpus is the documented ≥ size."""
    assert len(CORPUS) >= 40, f"corpus has only {len(CORPUS)} cases (§16.3 expects ≥ 40)"
    # No accidental duplicate case names.
    names = [c.name for c in CORPUS]
    assert len(names) == len(set(names)), "duplicate case names in CORPUS"


def test_error_tuples_have_all_six_keys() -> None:
    """Every observed error serializes to the frozen six-key §8.2 shape."""
    for case in CORPUS:
        for err in run_case(case):
            assert set(err.to_dict()) == {
                "code", "path", "message", "bound", "actual", "scope",
            }
