# Janus-Ops — developer and release entry points. `make help` lists the targets.
SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

VERSION   := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' backend/src/janus_ops/__init__.py)
IMAGE     ?= lordraw77/janus-ops
# COMPOSE_EXTRA adds override files, e.g. the signed agents and the OpenBao of this stack:
#   make docker/run COMPOSE_EXTRA="-f deploy/compose.release.yaml -f deploy/compose.openbao.yaml"
COMPOSE_EXTRA ?=
COMPOSE   := docker compose -f deploy/compose.yaml -f deploy/compose.build.yaml $(COMPOSE_EXTRA) --env-file deploy/.env

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z/_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- local development
.PHONY: deps
deps: ## Install backend (uv) and frontend (npm) dependencies
	cd backend && uv sync --frozen
	cd frontend && npm ci --no-audit --no-fund

.PHONY: build
build: deps ## Build the web UI static export (frontend/out)
	cd frontend && npm run build

.PHONY: lint
lint: ## Lint and type-check backend and frontend
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd frontend && npm run lint && npm run typecheck

.PHONY: test
test: ## Unit tests (no external services)
	cd backend && uv run pytest -m "not integration"

# Go runs in a container when it is not installed locally
GO := $(if $(shell command -v go 2>/dev/null),go,docker run --rm -v $(CURDIR)/agent:/src -w /src -e CGO_ENABLED=0 -e GOOS -e GOARCH golang:1.27.4-bookworm go)

.PHONY: agent/test
agent/test: ## Agent: gofmt check, vet and tests
	cd agent && test -z "$$($(GO)fmt -l . 2>/dev/null || true)"
	cd agent && $(GO) vet ./... && $(GO) test ./...

.PHONY: agent/build
agent/build: ## Agent: static binaries for amd64, arm64 and arm (32-bit Raspberry Pi) in agent/dist
	for arch in amd64 arm64 arm; do \
	  cd $(CURDIR)/agent && GOOS=linux GOARCH=$$arch GOARM=7 $(GO) build -trimpath \
	    -ldflags "-s -w -X main.version=$(VERSION)" -o dist/janus-agent-linux-$$arch ./cmd/janus-agent; \
	done

# The release key lives in secrets/release/release.pem: a git-ignored folder that is also outside
# the Docker build context (see docs/AGENT_RELEASE.md). Never commit it, never mount it into the stack.
.PHONY: agent/sign
agent/sign: agent/build ## Build the agent and sign agent/dist with secrets/release/release.pem
	@test -f secrets/release/release.pem || { echo "no release key: put it in secrets/release/release.pem" >&2; exit 1; }
	cd backend && uv run janus-ops agent-sign --private-key ../secrets/release/release.pem \
	  --dir ../agent/dist --agent-version $(VERSION)

.PHONY: test/integration
test/integration: ## Integration tests against a throwaway TimescaleDB container
	scripts/integration-test.sh

.PHONY: run
run: ## Run the backend locally (needs JANUS_DATABASE_URL); UI dev server: make run/ui
	cd backend && uv run janus-ops serve

.PHONY: run/ui
run/ui: ## Next.js dev server on :3000, proxying /api to the local backend
	cd frontend && npm run dev

# ---------------------------------------------------------------- docker
.PHONY: env
env: ## Create deploy/.env with random secrets (never overwrites)
	scripts/gen-env.sh

deploy/.env:
	@echo "deploy/.env missing: run 'make env' first" >&2; exit 1

.PHONY: docker/build
docker/build: ## Build the image $(IMAGE):$(VERSION)
	docker build --build-arg VERSION=$(VERSION) -t $(IMAGE):$(VERSION) .

.PHONY: docker/run
docker/run: deploy/.env ## Build and start the full stack (proxy, app, db, ldap)
	JANUS_VERSION=$(VERSION) $(COMPOSE) up -d --build

.PHONY: docker/stop
docker/stop: deploy/.env ## Stop the stack (volumes are kept)
	$(COMPOSE) down

.PHONY: breakglass
breakglass: deploy/.env ## Create the break-glass admin or rotate its password (prompts)
	$(COMPOSE) run --rm --no-deps janus-ops breakglass

.PHONY: logs
logs: deploy/.env ## Follow the stack logs
	$(COMPOSE) logs -f --tail=100

# ---------------------------------------------------------------- release
.PHONY: release
release: ## Tag v$(VERSION) and push the tag; CI publishes the image to Docker Hub
	scripts/release.sh "$(VERSION)"
