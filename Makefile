# DataForge — developer convenience targets.
#
# These are local-developer helpers; CI invokes uv/pytest directly. The one
# load-bearing target here is `golden-regen`, the *only* sanctioned way to move the
# committed GOLD-A determinism baseline (testing-strategy §6). CI never regenerates
# goldens — it only replays them — so a golden change must come from this target in
# a PR labelled `golden-rebaseline`.

BACKEND := backend

.PHONY: help golden-regen golden property property-nightly ops-e7 guards check soak

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
	@echo "  soak             SOAK-200: 200-TPS 1-hour soak vs the live compose stack"
	@echo "                   (attended Phase-6 gate / nightly; needs Kafka+ws+Redis,"
	@echo "                   websocat, and \$$ACCESS/\$$WS/\$$KEY/\$$STREAM env vars)."

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

# SOAK-200 (testing-strategy §13.1; phase-06 exit #5) — COMPOSE-ONLY, attended gate
# + nightly lane, never the PR lane (a 60-minute run). Needs the full live stack
# (Kafka + the ws ASGI process + the Redis channel layer + runner + sink host) and
# `websocat` for the independent WS tail consumer. Provide a started 200-TPS
# SEED_SOAK stream + its auth via env (the verify agent's demo-phase06.sh bootstraps
# them): ACCESS (JWT), WS (workspace id), KEY (events:read api key), STREAM (id).
# SOAK_MINUTES overrides the default 60 (use a small value for an attended smoke).
SOAK_MINUTES ?= 60
soak:
	python3 infra/scripts/soak200.py \
	  --access-token "$(ACCESS)" --workspace "$(WS)" --api-key "$(KEY)" \
	  --stream-id "$(STREAM)" --minutes $(SOAK_MINUTES)
