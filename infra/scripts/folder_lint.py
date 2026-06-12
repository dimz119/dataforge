#!/usr/bin/env python3
"""DataForge folder lint — Phase 1 exit criterion 5.

Asserts the repository's top-level layout matches D19
(specs/07-plan/project-folder-structure.md §1, rule FS-1): exactly four
top-level code/content directories (backend/, frontend/, infra/, specs/)
plus the allowed root files below. Any stray top-level entry or any missing
required entry exits 1.

Usage:
    python3 infra/scripts/folder_lint.py              # strict (CI, default)
    python3 infra/scripts/folder_lint.py --bootstrap  # Phase 1 scaffolding only

``--bootstrap`` tolerates *missing* backend/ and frontend/ while sibling
agents are still scaffolding them; it never tolerates stray entries. CI runs
strict mode.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The allowlists below are the machine-readable form of
# specs/07-plan/project-folder-structure.md §1 ("Repository root"). Changing
# the tree requires changing that document first (rule FS-1: a fifth top-level
# directory requires a superseding ADR referencing ADR-0001).
REQUIRED_DIRS: frozenset[str] = frozenset(
    {
        "backend",
        "frontend",
        "infra",
        "specs",
        ".github",
    }
)

REQUIRED_FILES: frozenset[str] = frozenset(
    {
        "README.md",
        "LICENSE",
        ".editorconfig",
        ".gitignore",
        ".pre-commit-config.yaml",
    }
)

# Allowed but not required at the top level.
ALLOWED_OPTIONAL: frozenset[str] = frozenset(
    {
        "PLAN.md",  # project planning doc (pre-dates the scaffold)
        "pnpm-workspace.yaml",  # listed in D19 §1; materializes if pnpm is adopted
        ".git",
        ".gitleaks.toml",
        # Local, gitignored tooling/scratch entries (never committed):
        ".omc",
        ".claude",
        ".vscode",
        ".idea",
        ".DS_Store",
    }
)

# Entries --bootstrap may report as missing without failing (Phase 1 only,
# while sibling agents scaffold them concurrently).
BOOTSTRAP_TOLERATED_MISSING: frozenset[str] = frozenset({"backend", "frontend"})


def lint(bootstrap: bool) -> int:
    errors: list[str] = []
    notes: list[str] = []
    entries = {p.name: p for p in REPO_ROOT.iterdir()}

    for name in sorted(REQUIRED_DIRS):
        if name not in entries:
            if bootstrap and name in BOOTSTRAP_TOLERATED_MISSING:
                notes.append(
                    f"bootstrap: required directory '{name}/' is missing — "
                    "tolerated only because --bootstrap was passed "
                    "(a sibling Phase 1 agent scaffolds it); CI runs strict."
                )
            else:
                errors.append(f"missing required top-level directory: {name}/")
        elif not entries[name].is_dir():
            errors.append(f"top-level entry '{name}' must be a directory")

    for name in sorted(REQUIRED_FILES):
        if name not in entries:
            errors.append(f"missing required top-level file: {name}")
        elif not entries[name].is_file():
            errors.append(f"top-level entry '{name}' must be a regular file")

    allowed = REQUIRED_DIRS | REQUIRED_FILES | ALLOWED_OPTIONAL
    for name in sorted(entries):
        if name not in allowed:
            errors.append(
                f"stray top-level entry: '{name}' — not in "
                "specs/07-plan/project-folder-structure.md §1 (rule FS-1)"
            )

    for note in notes:
        print(f"folder-lint NOTE: {note}")
    for error in errors:
        print(f"folder-lint FAIL: {error}", file=sys.stderr)

    if errors:
        print(
            f"folder-lint: {len(errors)} violation(s); tree must match "
            "specs/07-plan/project-folder-structure.md (D19)",
            file=sys.stderr,
        )
        return 1
    print("folder-lint OK: top-level tree matches project-folder-structure.md (D19)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="tolerate missing backend/ and frontend/ (Phase 1 scaffolding only)",
    )
    args = parser.parse_args()
    return lint(bootstrap=args.bootstrap)


if __name__ == "__main__":
    sys.exit(main())
