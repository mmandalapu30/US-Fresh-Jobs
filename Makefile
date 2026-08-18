# Developer entry points. Every target is safe to run repeatedly.
.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose --env-file .env.development
ALEMBIC := python -m alembic -c database/migrations/alembic.ini

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- environment

.PHONY: init
init: ## First-time setup: create .env.development if absent
	@test -f .env.development || (cp .env.example .env.development && \
	  echo "Created .env.development from template — set the CHANGE_ME values.")
	@echo "Ready. Next: make up && make migrate"

.PHONY: up
up: ## Start postgres, redis, minio and the api
	$(COMPOSE) up -d --build postgres redis minio minio-init api
	@echo "API      http://localhost:$${API_PORT_HOST:-8000}/docs"
	@echo "MinIO    http://localhost:$${MINIO_CONSOLE_PORT_HOST:-9001}"

.PHONY: up-workers
up-workers: ## Also start the celery worker and beat (Milestone 3+)
	$(COMPOSE) --profile workers up -d --build

.PHONY: down
down: ## Stop all services (volumes preserved)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop services AND DELETE ALL LOCAL DATA
	@read -p "This deletes the local database and object storage. Type 'yes': " ok; \
	  [ "$$ok" = "yes" ] || (echo "Aborted."; exit 1)
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail service logs
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

# ----------------------------------------------------------------- database

.PHONY: migrate
migrate: ## Apply all pending migrations
	$(ALEMBIC) upgrade head

.PHONY: migrate-sql
migrate-sql: ## Print the SQL a migration would run, without applying it
	$(ALEMBIC) upgrade head --sql

.PHONY: migrate-down
migrate-down: ## Roll back exactly one migration
	$(ALEMBIC) downgrade -1

.PHONY: migration
migration: ## Create a new empty migration: make migration m="add x"
	@test -n "$(m)" || (echo "Usage: make migration m=\"describe the change\""; exit 1)
	$(ALEMBIC) revision -m "$(m)"

.PHONY: db-shell
db-shell: ## Open psql against the dev database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-jobplatform} -d $${POSTGRES_DB:-jobplatform}

# -------------------------------------------------------------------- quality

.PHONY: install
install: ## Install all packages editable, with dev extras
	python -m pip install -e ./packages/shared -e ./packages/schemas
	python -m pip install -e "./apps/api[dev]"
	python -m pip install -e "./workers/ingestion[dev]"

.PHONY: lint
lint: ## Ruff check
	python -m ruff check .

.PHONY: format
format: ## Ruff format + autofix
	python -m ruff format .
	python -m ruff check --fix .

.PHONY: typecheck
typecheck: ## mypy
	python -m mypy packages apps/api/app workers/ingestion/ingestion

.PHONY: test
test: ## Run the full test suite
	python -m pytest

.PHONY: test-unit
test-unit: ## Unit tests only (no database or network)
	python -m pytest -m "not integration and not network"

.PHONY: test-cov
test-cov: ## Test suite with a coverage report
	python -m pytest --cov=packages --cov=apps/api/app --cov=workers/ingestion/ingestion --cov-report=term-missing

.PHONY: layering
layering: ## Verify no provider knowledge leaks outside its connector
	python scripts/check_layering.py

.PHONY: layering-rules
layering-rules: ## Show the source-isolation rules
	python scripts/check_layering.py --list

.PHONY: check
check: lint layering typecheck test ## Everything CI runs

# ------------------------------------------------------------------- source

.PHONY: daily
daily: ## Pull new jobs then enforce the freshness window (the 09:00 scheduled target)
	./scripts/daily.sh

.PHONY: watch
watch: ## Ingest only, and only if the source has published a file we do not have
	./scripts/daily.sh --watch

.PHONY: catch-up
catch-up: ## Ingest only, and only if today's file has not already landed
	./scripts/daily.sh --catch-up

.PHONY: has-new
has-new: ## Ask whether the source has published a new file (exit 1 yes, 0 no)
	python scripts/has_new_file.py

.PHONY: have-today
have-today: ## Ask whether today's delta file is already ingested (exit 0 yes, 1 no)
	python scripts/have_todays_file.py

.PHONY: retention
retention: ## Show what the freshness window would remove (dry run)
	python scripts/enforce_retention.py

.PHONY: verify-source
verify-source: ## Re-verify the live OpenJobData bucket against docs/00-source-verification.md
	python scripts/verify_source.py
