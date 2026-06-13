"""Fixtures for the OPS suite (testing-strategy §11).

Re-exports the two-workspace factory so the revocation-latency stopwatch can mint
keys in a real workspace, exactly as the demo does.
"""

from __future__ import annotations

from tenancy.tests.conftest import (  # noqa: F401  (re-exported as fixtures)
    make_user,
    make_workspace,
    password,
)
