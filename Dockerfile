# syntax=docker/dockerfile:1
# Janus-Ops single image: FastAPI control plane + statically exported web UI (ADR-009)

ARG NODE_IMAGE=node:24.22.3-alpine3.24
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.31
ARG GO_IMAGE=golang:1.27.4-bookworm
ARG DOCKER_CLI_IMAGE=docker:27.5.1-cli

# ---------------------------------------------------------------- web UI (static export)
FROM ${NODE_IMAGE} AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY res/ /src/res/
COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---------------------------------------------------------------- janus-agent (Go)
# Static binaries for every supported node architecture, cross-compiled on the build host;
# the server copies the right one to a node during the SSH bootstrap.
FROM --platform=$BUILDPLATFORM ${GO_IMAGE} AS agent
WORKDIR /src/agent
COPY agent/go.mod agent/go.sum ./
RUN go mod download
COPY agent/ ./
ARG VERSION=dev
RUN for arch in amd64 arm64 arm; do \
      CGO_ENABLED=0 GOOS=linux GOARCH=$arch GOARM=7 go build -trimpath -buildvcs=false \
        -ldflags "-s -w -X main.version=${VERSION}" \
        -o /out/janus-agent-linux-$arch ./cmd/janus-agent; \
    done; \
    CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -buildvcs=false \
      -ldflags "-s -w -X main.version=${VERSION}" \
      -o /out/janus-agent-windows-amd64.exe ./cmd/janus-agent

# Signatures made offline with the release key (`make agent/sign`, committed in agent/release/) ship
# next to the binaries, which is what lets the server push signed updates (FR-84). The build is
# reproducible, so a signature made on the release machine matches these exact bytes; the build refuses
# a signature for other bytes or another version. With REQUIRE_SIGNED=true (CI on release tags) it
# also refuses to build without one. The private key never enters the build.
ARG REQUIRE_SIGNED=false
RUN set -eu; \
    for arch in amd64 arm64 arm; do \
      bin=/out/janus-agent-linux-$arch; sig=release/janus-agent-linux-$arch.sig; \
      if [ ! -f "$sig" ]; then \
        if [ "$REQUIRE_SIGNED" = "true" ]; then \
          echo "no signature for $arch: run 'make agent/sign' and commit agent/release/" >&2; exit 1; \
        fi; \
        echo "no signature for $arch: this image will not be able to update agents"; continue; \
      fi; \
      want=$(grep -o '"sha256": *"[0-9a-f]*"' "$sig" | grep -o '[0-9a-f]\{64\}'); \
      ver=$(grep -o '"version": *"[^"]*"' "$sig" | sed 's/.*: *"\(.*\)"/\1/'); \
      have=$(sha256sum "$bin" | cut -d' ' -f1); \
      if [ "$want" != "$have" ] || [ "$ver" != "${VERSION}" ]; then \
        echo "the signature for $arch is for other bytes or another version ($ver, want ${VERSION}): run 'make agent/sign'" >&2; exit 1; \
      fi; \
      cp "$sig" "$bin.sig"; \
    done

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
FROM ${DOCKER_CLI_IMAGE} AS dockercli
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="Janus-Ops" \
      org.opencontainers.image.description="Unified infrastructure operations portal" \
      org.opencontainers.image.source="https://github.com/lordraw77/janus-ops" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

# git: Git playbook sources; openssh-client: ansible-playbook and the SSH deploy keys;
# sshpass: Ansible's password authentication for password credentials
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client sshpass \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 janus \
 && useradd --system --uid 10001 --gid janus --home-dir /app --shell /usr/sbin/nologin janus \
 && install -d -o 10001 -g 10001 -m 0700 /var/lib/janus-ops/recordings

# docker CLI only: the Copilot starts the MCP servers (linux-mcp, proxmox-mcp) with `docker run -i`
# when deploy/compose.mcp.yaml gives it a Docker socket; without it the CLI is unused
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=backend --chown=root:root /app/.venv /app/.venv
COPY --from=ui --chown=root:root /src/frontend/out /app/static
COPY --from=agent --chown=root:root /out /app/agent

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JANUS_STATIC_DIR=/app/static \
    JANUS_AGENT_DIR=/app/agent \
    JANUS_PORT=8000

WORKDIR /app
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('JANUS_PORT','8000')}/api/v1/health\", timeout=4)"]

ENTRYPOINT ["janus-ops"]
CMD ["serve"]
