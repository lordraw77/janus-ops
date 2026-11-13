# syntax=docker/dockerfile:1
# Janus-Ops single image: FastAPI control plane + statically exported web UI (ADR-009)

ARG NODE_IMAGE=node:24.22.3-alpine3.24
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.31

# ---------------------------------------------------------------- web UI (static export)
FROM ${NODE_IMAGE} AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY res/ /src/res/
COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---------------------------------------------------------------- Python virtualenv
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS backend
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src/backend
# Dependencies first (cached layer), then the project itself
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="Janus-Ops" \
      org.opencontainers.image.description="Unified infrastructure operations portal" \
      org.opencontainers.image.source="https://github.com/lordraw77/janus-ops" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

RUN groupadd --system --gid 10001 janus \
 && useradd --system --uid 10001 --gid janus --home-dir /app --shell /usr/sbin/nologin janus

COPY --from=backend --chown=root:root /app/.venv /app/.venv
COPY --from=ui --chown=root:root /src/frontend/out /app/static

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JANUS_STATIC_DIR=/app/static \
    JANUS_PORT=8000

WORKDIR /app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('JANUS_PORT','8000')}/api/v1/health\", timeout=4)"]

ENTRYPOINT ["janus-ops"]
CMD ["serve"]
