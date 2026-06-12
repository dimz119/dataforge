# DataForge backend image — multi-target (deployment-architecture §8.1).
# Targets: base → deps → dev (compose/CI, bind-mount friendly) → runtime/prod
# (gunicorn; the one production image for all four process groups).

# --- base: OS deps (libpq, librdkafka), non-root user, uv ---------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 librdkafka1 curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 dataforge \
    && useradd --uid 1000 --gid dataforge --create-home dataforge

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

WORKDIR /app

# --- deps: production dependency set (uv sync --frozen) -----------------------
FROM base AS deps

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# --- dev: dev/test extras, no code copy (compose bind-mounts /app) ------------
FROM deps AS dev

RUN uv sync --frozen
ENV DJANGO_SETTINGS_MODULE=config.settings.dev
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# --- runtime: the production image for all four process groups ----------------
FROM deps AS runtime

COPY . /app
# The console SPA enters as a CI build artifact dropped at backend/frontend-dist
# before the image build (deployment-architecture §8.1); the glob keeps local
# builds without the artifact working.
COPY frontend-dis[t]/ /app/static/spa/

RUN DJANGO_SETTINGS_MODULE=config.settings.dev python manage.py collectstatic --noinput

USER dataforge
ENV DJANGO_SETTINGS_MODULE=config.settings.prod
EXPOSE 8000
# Default command = web process group; ws/worker/runner override it
# (deployment-architecture §3.2 process table).
CMD ["gunicorn", "config.wsgi:application", "-b", "0.0.0.0:8000", "-w", "4", "--timeout", "60"]

# Alias so compose/CI can reference target `prod` (Phase 1 cross-stack contract).
FROM runtime AS prod
