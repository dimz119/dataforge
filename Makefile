# DataForge — developer convenience targets.
#
# These are local-developer helpers; CI invokes uv/pytest directly. The one
# load-bearing target here is `golden-regen`, the *only* sanctioned way to move the
# committed GOLD-A determinism baseline (testing-strategy §6). CI never regenerates
# goldens — it only replays them — so a golden change must come from this target in
# a PR labelled `golden-rebaseline`.

BACKEND := backend

.PHONY: help golden-regen golden property property-nightly ops-e7 guards check

help:
	@echo "DataForge make targets:"
	@echo "  golden-regen     Regenerate the committed GOLD-A fixture (local only;"
	@echo "                   requires the 'golden-rebaseline' PR label, §6)."
	@echo "  golden           Run the GOLD-A byte-identity replay suite."
	@echo "  property         Run PROP-RI-1..8 over the 100k PR profile."
	@echo "  property-nightly Run PROP-RI-1..8 over the 1M nightly/gate profile."
	@echo "  ops-e7           Run the OPS-11 DuckDB / Exercise-E7 round-trip."
	@echo "  guards           Run the GUARD suite (incl. engine genericity, BE-T1)."
	@echo "  check            ruff + mypy + lint-imports + the default test lane."

# Regenerate the GOLD-A golden fixture. Local use ONLY (testing-strategy §6):
# regenerating a committed golden is a deliberate, reviewable re-baseline. Open the
# PR with the `golden-rebaseline` label and explain which intentional change altered
# the determinism unit's output. CI replays the fixture; it never runs this target.
golden-regen:
	cd $(BACKEND) && uv run python -m tests.golden.regen

golden:
	cd $(BACKEND) && uv run pytest -m golden -q

property:
	cd $(BACKEND) && uv run pytest -m property -q

property-nightly:
	cd $(BACKEND) && uv run pytest -m property_nightly -q

ops-e7:
	cd $(BACKEND) && uv run pytest tests/ops/test_ops11_duckdb_e7.py -q

guards:
	cd $(BACKEND) && uv run pytest -m guards -q

check:
	cd $(BACKEND) && uv run ruff check . && uv run mypy . && uv run lint-imports && uv run pytest
