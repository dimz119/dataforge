# Phase 1 — Foundations

**Deliverable:** D18 (phase doc)

Phase 1 erects the complete skeleton: a monorepo that boots the **final** nine-service topology — Kafka included, per ADR-0005 — under Docker Compose, with CI, health probes, and a throwaway Fly.io deploy. The point is shape-finality: no later phase changes the infrastructure shape; later phases only replace stub commands with real ones (deployment-architecture principle D-1). Deployment is de-risked on day one, not at GA.

## Goal

> A booting, CI-verified monorepo with the final infrastructure topology (Kafka included) so no later phase changes the shape; deployment de-risked immediately.

## Dependencies

| Dependency | Role |
|---|---|
| Phase 0 complete (specs approved) | No code before design approval |
| [../../02-architecture/deployment-architecture.md](../../02-architecture/deployment-architecture.md) | Compose stack §2 (service table, KRaft config, volumes), CI/CD pipeline, Fly.io topology |
| [../../02-architecture/backend-architecture.md](../../02-architecture/backend-architecture.md) | Process inventory, settings split, app-per-bounded-context layout |
| [../../02-architecture/frontend-architecture.md](../../02-architecture/frontend-architecture.md) | Vite + React + TS shell, routing skeleton |
| [../../02-architecture/observability.md](../../02-architecture/observability.md) | `/healthz` (liveness, no dependency checks) vs `/readyz` (gated dependency probes) semantics |
| [../project-folder-structure.md](../project-folder-structure.md) | D19 — the tree the folder-lint enforces |
| ADR-0001 (monorepo), ADR-0005 (Kafka day one), ADR-0015 (Fly.io process groups) | Structural decisions implemented here |

## Scope

- **Monorepo scaffold:** `backend/` (Django + DRF project, settings split base/dev/test/prod, **custom User placeholder** set as `AUTH_USER_MODEL` before the first migration, the ten app skeletons per domain-model §1.3: `identity`, `tenancy`, `catalog`, `registry`, `streams`, `generation`, `chaos`, `delivery`, `observation`, `audit`); `frontend/` (Vite React + TS shell with router skeleton and placeholder pages); `infra/` (compose, fly, ci, loadtest, scripts) — tree exactly per D19.
- **Docker Compose at `infra/compose/`:** the nine platform services of deployment-architecture §2.1 — `postgres`, `redis`, `kafka` (single-node KRaft, fixed `CLUSTER_ID`), `api`, `ws`, `worker` (Celery), `runner` (Phase-1 stub: heartbeats + health listener on :8081, host-mapped 8090), `buffer-writer` (stub, health on :8081, host-mapped 8091), `web` — plus the dev-only `mailpit` mail-capture container (exempt from the nine-service parity count per §2.1) — with healthchecks, named volumes, and the dev-only `migrate` + `provision_kafka_topics` entrypoint creating `df.delivery.events.v1` (12 partitions, backend-architecture topic layout).
- **Probes:** `/healthz` and `/readyz` on every backend process; `/readyz` probes Postgres/Redis/Kafka with 2 s timeouts and 5 s result cache, returning the per-component JSON map observability §6 defines.
- **CI (single path-filtered pipeline, ADR-0001):** backend job (ruff, mypy, `lint-imports` per backend-architecture §3.2, pytest), frontend job (eslint, tsc, vitest), **OpenAPI artifact job** (drf-spectacular schema generated, committed, and diffed — the ADR-0014 lockstep mechanism starts now), pre-commit hooks incl. gitleaks with the `df_` key rule (SEC-KEY-1).
- **Folder-lint:** a script asserting the working tree matches [../project-folder-structure.md](../project-folder-structure.md), run in CI.
- **Throwaway Fly.io deploy:** one app, `web` process group only, serving `/healthz` — proves registry auth, secrets handling, and the deploy pipeline end to end.

## Non-goals

| Deferred | Lands in |
|---|---|
| Auth, users beyond the placeholder model, workspaces, API keys | Phase 2 |
| Any domain model, manifest, or schema row | Phases 3–4 |
| Events through Kafka (topics exist; nothing produces) | Phase 5 |
| Real runner / buffer-writer logic (stubs heartbeat + serve health only) | Phases 5–6 |
| Frontend pages beyond the routed shell | Phase 7 |
| Full Fly.io process-group topology (`ws`/`worker`/`runner`), managed-Kafka trigger | Phase 11 production posture (ADR-0015) |

## Tasks

- [ ] Repo layout: `backend/`, `frontend/`, `infra/`, root README quickstart, license, editorconfig — tree per D19
- [ ] Django project: settings split, custom User placeholder + initial migration, `/healthz` + `/readyz` function views
- [ ] Ten Django app skeletons with empty `models.py`, `tests/` dirs, and registration in the three settings app lists
- [ ] Frontend: Vite + React + TS scaffold, React Router skeleton, placeholder route components, vitest smoke test
- [ ] Compose: `postgres`, `redis`, `kafka` (KRaft, fixed `CLUSTER_ID`, dual listeners 9092/19092) with healthchecks + volumes
- [ ] Compose: `api`, `worker`, `ws` services with dev bind mounts and healthchecks
- [ ] Runner stub (`python -m runner --role generation`) and buffer-writer stub (`python -m runner --role sinks`): heartbeat loop + aiohttp health listener on :8081 (backend-architecture §8.1)
- [ ] `/readyz` dependency probes (pg/redis/kafka, 2 s timeout, 5 s cache) wired per process gating set
- [ ] `provision_kafka_topics` management command (idempotent; `df.delivery.events.v1`, 12 partitions)
- [ ] Import-linter contracts in `pyproject.toml` (backend-architecture §3.2) + `lint-imports` CI wiring
- [ ] Backend CI job: ruff + mypy + pytest against compose-provided Postgres/Redis/Kafka
- [ ] Frontend CI job: eslint + tsc + vitest
- [ ] OpenAPI artifact job: generate, commit, fail on dirty diff
- [ ] Pre-commit config: ruff, prettier, gitleaks (`df_` rule)
- [ ] Folder-lint script (`infra/scripts/folder_lint.py`) + CI wiring
- [ ] Backend multi-stage Dockerfile (dev/prod targets) and frontend Dockerfile
- [ ] Fly.io app + hello-world deploy of `web` process group; post-deploy smoke script

## Demo script

1. `git clone <repo> && cd dataforge && cp infra/compose/.env.example infra/compose/.env`
2. `docker compose -f infra/compose/compose.yaml up -d --wait` — exits 0 with all nine platform services (plus the dev-only `mailpit` container) started.
3. `docker compose -f infra/compose/compose.yaml ps` — ten containers (nine platform services + `mailpit`), every one `healthy`.
4. `curl -fsS localhost:8000/healthz` → `200`; `curl -fsS localhost:8000/readyz | jq` → `postgres`, `redis`, `kafka` all `"ok"`, HTTP 200.
5. `curl -fsS localhost:8090/healthz && curl -fsS localhost:8091/healthz` — runner and buffer-writer stubs alive.
6. `kcat -b localhost:19092 -L` — broker up; topic `df.delivery.events.v1` listed with 12 partitions.
7. Open `http://localhost:5173` — the frontend shell renders with the routing skeleton.
8. Open a trivial PR (e.g. README typo): CI runs lint/type/test on both stacks, the OpenAPI artifact job, and folder-lint — all green.
9. `flyctl deploy --config infra/fly/fly.toml` then `curl -fsS https://<app>.fly.dev/healthz` → `200`.
10. `python infra/scripts/folder_lint.py` → exit 0.
11. `docker compose -f infra/compose/compose.yaml down && docker compose -f infra/compose/compose.yaml up -d --wait` — stack survives restart with volumes intact (fixed `CLUSTER_ID`: Kafka does not reformat).

## Exit criteria

Binding text with measurable assertions; proving suites per [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §14 (Phase 1 rows).

| # | Binding criterion | Measurable assertion | Proving suite (lane) |
|---|---|---|---|
| 1 | "docker compose up brings all services healthy" | All **nine** platform services (plus the dev-only `mailpit` container) reach `healthy` within their configured healthcheck windows from a cold start; survives `down`/`up` without reformatting Kafka | OPS smoke: compose health poller (PR) |
| 2 | "readyz reports green for pg/redis/kafka" | `GET /readyz` on `api` returns 200 with `postgres/redis/kafka = ok` in the component map; probe timeout 2 s, cache 5 s per observability §6 | OPS smoke: readyz JSON assertion (PR) |
| 3 | "CI green on a trivial PR" | STATIC (ruff/mypy/lint-imports/eslint/tsc/prettier) + UNIT smoke + OpenAPI artifact job + pre-commit all pass on a no-op PR; dirty OpenAPI diff demonstrably fails a planted-change PR | STATIC + CON schema job (PR) |
| 4 | "Fly URL serves healthz" | `https://<app>.fly.dev/healthz` returns 200 from the deployed `web` process group | post-deploy smoke script (merge) |
| 5 | "tree matches D19" | Folder-lint exits 0 against [../project-folder-structure.md](../project-folder-structure.md); a planted stray top-level directory makes it exit non-zero | folder-lint script (merge) |
