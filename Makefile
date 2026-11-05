# Scylla DBaaS - developer entry points. `make help` lists them.

SHELL := /bin/bash
.DEFAULT_GOAL := help

CP      := control-plane
CQL     := cql-client
API_URL ?= http://localhost:8000
API_KEY ?= dev-key
ROWS    ?= 1000

PY_PACKAGES := $(CP) $(CQL)

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---- setup ----------------------------------------------------------------------------

$(CP)/.venv/.installed: $(CP)/pyproject.toml
	cd $(CP) && uv venv -q --allow-existing --python 3.12 && uv pip install -q -e '.[dev,aws]'
	@touch $@

$(CQL)/.venv/.installed: $(CQL)/pyproject.toml
	cd $(CQL) && uv venv -q --allow-existing --python 3.12 && uv pip install -q -e '.[dev]'
	@touch $@

.PHONY: install
install: $(CP)/.venv/.installed $(CQL)/.venv/.installed ## Create Python venvs (uv)
	@if [ -f ui/package.json ]; then (cd ui && npm ci); fi

# ---- run ------------------------------------------------------------------------------

.PHONY: dev
dev: ## Start the Control Plane + UI dev stack (docker compose)
	docker compose up --build

.PHONY: down
down: ## Stop the dev stack
	docker compose down

.PHONY: api-fake
api-fake: $(CP)/.venv/.installed ## Run the API on the host with the in-memory fake Provider
	cd $(CP) && DBAAS_PROVIDER=fake .venv/bin/uvicorn dbaas.main:app --reload --port 8000

.PHONY: api-local
api-local: $(CP)/.venv/.installed ## Run the API on the host with the local (Docker) Provider
	cd $(CP) && DBAAS_PROVIDER=local .venv/bin/uvicorn dbaas.main:app --port 8000

# ---- quality --------------------------------------------------------------------------

.PHONY: test
test: $(CP)/.venv/.installed $(CQL)/.venv/.installed ## Run unit tests (integration tests need DBAAS_INTEGRATION=1)
	cd $(CP) && .venv/bin/pytest -q
	cd $(CQL) && .venv/bin/pytest -q

.PHONY: lint
lint: $(CP)/.venv/.installed $(CQL)/.venv/.installed ## ruff + mypy, terraform fmt, UI lint/typecheck, actionlint (when installed)
	@for p in $(PY_PACKAGES); do \
	  echo "== $$p"; \
	  (cd $$p && .venv/bin/ruff check src tests && .venv/bin/mypy src) || exit 1; \
	done
	@if command -v terraform >/dev/null && [ -d infra ]; then \
	  echo "== terraform fmt"; terraform fmt -check -recursive infra; \
	else echo "-- skipping terraform (not installed or no infra/)"; fi
	@if [ -d ui/node_modules ]; then \
	  echo "== ui"; (cd ui && npm run -s lint && npm run -s typecheck); \
	else echo "-- skipping ui (run 'make install' first)"; fi
	@if command -v actionlint >/dev/null; then \
	  echo "== actionlint"; actionlint; \
	else echo "-- skipping actionlint (brew install actionlint)"; fi

# ---- demo -----------------------------------------------------------------------------

.PHONY: demo-local
demo-local: $(CQL)/.venv/.installed ## E2E demo on the local driver: create, write, verify, Backup, Restore, verify, delete
	@curl -fsS -o /dev/null "$(API_URL)/api/v1/healthz" || { \
	  echo "Control Plane not reachable at $(API_URL)."; \
	  echo "Start it first: 'make api-local' (or 'make dev' with DBAAS_PROVIDER=local)."; \
	  exit 1; }
	cd $(CQL) && .venv/bin/dbaas-e2e --api "$(API_URL)" --api-key "$(API_KEY)" \
	  --name demo-local --rows $(ROWS) --with-restore --timeout 1800 --poll 3

.PHONY: clean
clean: ## Remove venvs and caches
	rm -rf $(CP)/.venv $(CQL)/.venv
	find . -type d \( -name __pycache__ -o -name .mypy_cache -o -name .ruff_cache \
	  -o -name .pytest_cache \) -prune -exec rm -rf {} +
