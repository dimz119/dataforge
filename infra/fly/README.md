# Fly.io deployment (Phase 1 throwaway)

One Fly app, `web` process group only, serving `/healthz` — the Phase 1
deploy-pipeline proof (phase-01-foundations.md exit criterion 4). The full
process-group topology (`ws`/`worker`/`runner`) and `fly.kafka.toml` land in
Phase 11 per ADR-0015 and deployment-architecture §3.

## Deploy

From the **repo root** (the positional arg makes `backend/` the build
context, matching `[build] dockerfile = "Dockerfile"` in `fly.toml`):

```sh
flyctl deploy backend --config infra/fly/fly.toml
```

First-time setup:

```sh
flyctl apps create dataforge
flyctl secrets set --app dataforge \
  DJANGO_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')"
```

Post-deploy smoke (exit criterion: HTTP 200):

```sh
curl -fsS https://dataforge.fly.dev/healthz
```

Secrets live only in `fly secrets` (rule S-1/S-2, deployment-architecture §5);
`[env]` in `fly.toml` carries non-secret config only. CI deploys authenticate
with a scoped `FLY_API_TOKEN` GitHub environment secret.
