# Loyalty Points Transfer Engine — every developer command lives here. Run `make` for help.
#
# Lint, typecheck, tests and migrations run inside the `tools` container (Python 3.12, source
# mounted), so the only host requirements are Docker and make. To use a local virtualenv
# instead, override TOOLS with an empty value, e.g. `make test TOOLS=`.

.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE ?= docker compose
TOOLS ?= $(COMPOSE) run --rm --user "$$(id -u):$$(id -g)" tools
# Same as TOOLS but without starting PostgreSQL/Redis/simulator (lint, typecheck, unit tests).
TOOLS_NODEPS ?= $(COMPOSE) run --rm --no-deps --user "$$(id -u):$$(id -g)" tools

.PHONY: help up down logs ps build shell migrate makemigration seed check-invariants cleanup-idempotency psql reset test test-unit \
	test-integration coverage lint format typecheck diagrams demo run-local

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\nTargets:\n"} \
		/^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------- Docker stack
build: ## Build the runtime and dev (tools) images
	$(COMPOSE) --profile tools build

up: ## Build if needed, start postgres, redis, simulator, api and worker; wait until healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop and remove containers (keeps the database volume)
	$(COMPOSE) down --remove-orphans

logs: ## Follow logs of all services (e.g. `make logs s=worker` for one service)
	$(COMPOSE) logs -f $(s)

ps: ## Show service status and health
	$(COMPOSE) ps

shell: ## Open a shell in the tools container (source mounted, dev dependencies installed)
	$(TOOLS) bash

# ---------------------------------------------------------------- Database
migrate: ## Apply all database migrations (alembic upgrade head)
	$(TOOLS) alembic upgrade head

makemigration: ## Autogenerate a migration from model changes: make makemigration name=add_x
	@test -n "$(name)" || (echo "usage: make makemigration name=<short_description>" && exit 1)
	$(TOOLS) alembic revision --autogenerate -m "$(name)"

seed: ## Load seed data (idempotent, safe to run repeatedly)
	$(TOOLS) python -m scripts.seed

check-invariants: ## Verify ledger invariants (exit code 1 on any violation)
	$(TOOLS) python -m scripts.check_invariants

cleanup-idempotency: ## Delete expired idempotency keys (housekeeping; safe any time)
	$(TOOLS) python -m scripts.cleanup_idempotency

psql: ## Open psql on the running PostgreSQL container
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

reset: ## Drop all data, start the stack, migrate and seed
	$(COMPOSE) down --volumes --remove-orphans
	$(MAKE) up
	$(MAKE) migrate
	$(MAKE) seed

# ---------------------------------------------------------------- Quality
test: ## Run all tests (unit + integration; starts required services)
	$(TOOLS) pytest

test-unit: ## Run unit tests only (no services needed)
	$(TOOLS_NODEPS) pytest -m unit

test-integration: ## Run integration tests (PostgreSQL, Redis, partner simulator)
	$(TOOLS) pytest -m integration

coverage: ## Run all tests with a coverage report (terminal + htmlcov/); fails below 90%
	$(TOOLS) pytest --cov --cov-report=term-missing --cov-report=html

lint: ## Check style and common bugs with ruff (lint + format check)
	$(TOOLS_NODEPS) sh -c "ruff check . && ruff format --check ."

format: ## Auto-fix lint issues and format code with ruff
	$(TOOLS_NODEPS) sh -c "ruff check --fix . && ruff format ."

typecheck: ## Static type check with mypy (strict)
	$(TOOLS_NODEPS) mypy

# ---------------------------------------------------------------- Docs
MERMAID_CLI ?= npx -y @mermaid-js/mermaid-cli@11.4.2

diagrams: ## Render docs/diagrams/*.mmd to SVG and PNG in docs/images/ (needs Node.js 18+)
	@command -v npx >/dev/null || (echo "npx not found: install Node.js 18+ to render diagrams" && exit 1)
	@mkdir -p docs/images
	@for source in docs/diagrams/*.mmd; do \
		name=$$(basename $$source .mmd); \
		echo "rendering $$name"; \
		$(MERMAID_CLI) -q -i $$source -o docs/images/$$name.svg -b white || exit 1; \
		$(MERMAID_CLI) -q -i $$source -o docs/images/$$name.png -b white -s 2 || exit 1; \
	done

# ---------------------------------------------------------------- Demo / local
demo: ## End-to-end walkthrough of every scenario against the running stack (run make up first)
	$(TOOLS) python -m scripts.demo

run-local: ## Run the API on the host with auto-reload (needs a venv; services via `make up`)
	uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
