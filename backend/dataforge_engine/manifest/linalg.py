"""A minimal pure-Python linear solver for the V207 fundamental-matrix bound.

MAN-V207 computes the expected number of transitions from ``initial`` to
absorption in an absorbing Markov chain via the fundamental matrix
``N = (I - Q)^-1`` and the row sum ``t = N · 1`` (Grinstead & Snell). We only need
``t[initial]``, i.e. the solution of the linear system ``(I - Q) · t = 1`` — a
single solve, no full inverse.

We implement Gaussian elimination with partial pivoting **in pure Python** rather
than depend on numpy:

* the matrix is ≤ 40x40 (B-06: ≤ 40 transient states per machine), so an
  O(n³) ≤ 64,000-op solve is trivially fast;
* it runs once per machine at **publish** time (a cold control-plane path), never
  in the hot data plane;
* it keeps ``dataforge_engine`` dependency-light and avoids pulling a multi-MB
  binary wheel into the framework-free package for one cold call.

A near-singular ``(I - Q)`` (a guaranteed-infinite component, no path to
absorption) is reported by the caller as MAN-V205 (§8.2 MAN-V207 note); this
module signals it by raising :class:`SingularMatrixError`.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

# Pivot magnitude below which (I - Q) is treated as singular within tolerance
# (§8.2: "a singular (I - Q) within tolerance is reported as V205").
SINGULAR_TOLERANCE = 1e-12


class SingularMatrixError(Exception):
    """Raised when the coefficient matrix is singular within tolerance."""


def solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Solve the dense linear system ``A · x = b`` by Gaussian elimination.

    ``matrix`` is an ``nxn`` list of rows (mutated-on-copy), ``rhs`` length ``n``.
    Uses partial pivoting for numerical stability. Raises
    :class:`SingularMatrixError` if a pivot falls below
    :data:`SINGULAR_TOLERANCE`. Returns the solution vector ``x``.
    """
    n = len(matrix)
    if n == 0:
        return []
    if any(len(row) != n for row in matrix) or len(rhs) != n:
        raise ValueError("matrix must be square and rhs must match its dimension")

    # Build an augmented copy so the caller's data is untouched.
    aug: list[list[float]] = [[*list(matrix[i]), rhs[i]] for i in range(n)]

    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < SINGULAR_TOLERANCE:
            raise SingularMatrixError("coefficient matrix is singular within tolerance")
        if pivot_row != col:
            aug[col], aug[pivot_row] = aug[pivot_row], aug[col]

        pivot = aug[col][col]
        for r in range(col + 1, n):
            factor = aug[r][col] / pivot
            if factor == 0.0:
                continue
            row_r = aug[r]
            row_c = aug[col]
            for c in range(col, n + 1):
                row_r[c] -= factor * row_c[c]

    # Back-substitution.
    solution = [0.0] * n
    for row in range(n - 1, -1, -1):
        acc = aug[row][n]
        for c in range(row + 1, n):
            acc -= aug[row][c] * solution[c]
        solution[row] = acc / aug[row][row]
    return solution


def expected_steps_to_absorption(
    transient_order: list[str],
    sub_q: list[list[float]],
) -> float:
    """Expected transitions from the first transient state until absorption.

    ``transient_order`` lists the transient (non-absorbing) states; ``sub_q`` is
    the ``Q`` sub-matrix of transition probabilities among them (row i → col j).
    Returns ``t[0]`` of ``(I - Q) · t = 1`` — the initial state must be first in
    ``transient_order``. Raises :class:`SingularMatrixError` on a non-absorbing
    (singular) component.
    """
    n = len(transient_order)
    if n == 0:
        return 0.0
    i_minus_q = [
        [(1.0 if r == c else 0.0) - sub_q[r][c] for c in range(n)] for r in range(n)
    ]
    ones = [1.0] * n
    times = solve(i_minus_q, ones)
    return times[0]
