"""The adversarial manifest fixture corpus (testing-strategy §16.3).

This package is the single, authoritative corpus that proves Phase-3 exit
criterion #1: *every* ``MAN-S``/``MAN-V`` code the validator can emit has at least
one failing fixture, and each fixture pins the **exact** error tuple
``{code, path, bound, actual, scope}`` (scenario-plugin-architecture §8.2). It
complements (does not replace) the per-concern unit modules in ``tests/catalog/``
which assert one code family each; here the whole code set is enumerated in one
place so a missing fixture for a newly-emitted code fails the coverage meta-test
(``test_adversarial_corpus.py::test_every_emitted_code_has_a_fixture``).

Public surface:

* :class:`AdversarialCase` — one fixture: a name, the expected error tuple, the
  validation *flavor* (how the doc is fed to the validator), and a builder that
  returns the malformed document (a ``dict`` mutation of the valid base, or raw
  YAML/JSON text for the parse-hardening codes).
* :data:`CORPUS` — the ordered list of cases (≥ 1 per emitted code).
* :func:`run_case` — execute a case and return its observed
  :class:`~dataforge_engine.manifest.ValidationError` list.
"""

from __future__ import annotations

from tests.catalog.fixtures.base import valid_subset_manifest
from tests.catalog.fixtures.cases import CORPUS, AdversarialCase, run_case

__all__ = ["CORPUS", "AdversarialCase", "run_case", "valid_subset_manifest"]
