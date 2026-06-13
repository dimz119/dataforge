# DataForge

Synthetic data-stream platform: a Django/DRF control plane, a deterministic
generation data plane, and a React console, delivered as one monorepo
(ADR-0001). The full design corpus lives in [`specs/`](specs/README.md); the
implementation roadmap is in
[`specs/07-plan/`](specs/07-plan/project-folder-structure.md).

## Quickstart (dev stack)

Prerequisites: Docker (compose v2). The dev stack is the **final** nine-service
topology from Phase 1 onward (deployment-architecture principle D-1).

```sh
git clone <repo> && cd dataforge
cp infra/compose/.env.example infra/compose/.env
docker compose -f infra/compose/compose.yaml up -d --wait
```

All ten containers (nine platform services + dev-only Mailpit) report healthy;
verify with `docker compose -f infra/compose/compose.yaml ps`, or run the
scripted Phase 1 demo: `infra/scripts/demo-phase01.sh`.

| URL | What |
|---|---|
| <http://localhost:5173> | Console SPA (Vite dev server, proxies `/api` and `/ws`) |
| <http://localhost:8000> | REST API — `/api/v1`, `/healthz`, `/readyz` |
| <http://localhost:8001> | WebSocket tier (Channels) — `/healthz` |
| <http://localhost:8090/healthz> | `runner` health (data plane, debug only) |
| <http://localhost:8091/healthz> | `buffer-writer` health (sink host, debug only) |
| <http://localhost:8025> | Mailpit UI (captured dev email) |
| `localhost:19092` | Kafka host tooling listener (`kcat -b localhost:19092 -L`) |
| `localhost:5432` / `localhost:6379` | Postgres / Redis |

Full reset (drops the named volumes `pgdata`/`redisdata`/`kafkadata`):
`docker compose -f infra/compose/compose.yaml down -v`.

## Repository layout

Fixed by [specs/07-plan/project-folder-structure.md](specs/07-plan/project-folder-structure.md)
(D19) and enforced in CI by `infra/scripts/folder_lint.py`:

| Path | Contents |
|---|---|
| `backend/` | Django monolith + framework-free engine + runner |
| `frontend/` | Vite + React + TS console SPA |
| `infra/` | compose, Fly.io, CI scripts, load tests |
| `specs/` | the design deliverables (D1-D20), ADRs, phase docs |

## Local checks

```sh
# Backend
cd backend && uv sync && uv run ruff check . && uv run mypy . \
  && uv run lint-imports && uv run pytest

# Frontend
cd frontend && npm ci && npm run lint && npx tsc --noEmit && npm test -- --run

# Tree lint + hooks
python3 infra/scripts/folder_lint.py
pre-commit install   # ruff, prettier, gitleaks (df_ key rule)
```

CI runs the same commands in one path-filtered pipeline
([`.github/workflows/ci.yaml`](.github/workflows/ci.yaml)), plus the OpenAPI
artifact drift gate (ADR-0014). Deployment: see
[`infra/fly/README.md`](infra/fly/README.md).

## License

[MIT](LICENSE)

<!-- CI pipeline verification: trivial no-op change to prove the four CI jobs go green (phase-01 exit criterion 3). -->
