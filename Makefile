PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: help venv install dev-install up down logs psql migrate lint format typecheck test test-unit run executor-image clean

help: ## List available targets
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

venv: ## Create the virtual environment
	$(PYTHON) -m venv $(VENV)

install: ## Install runtime dependencies
	$(PIP) install -r requirements.txt

dev-install: ## Install runtime + dev dependencies (editable)
	$(PIP) install -e . -r requirements.txt -r requirements-dev.txt

up: ## Start infrastructure (PostgreSQL + Redis) and wait until healthy
	docker compose up -d --wait

down: ## Stop infrastructure
	docker compose down

logs: ## Tail infrastructure logs
	docker compose logs -f

psql: ## Open a psql shell against the local database
	docker compose exec postgres psql -U coding -d coding_agent

migrate: ## Apply all migrations
	$(VENV)/bin/alembic upgrade head

migrate-downgrade: ## Roll back the last migration
	$(VENV)/bin/alembic downgrade -1

migrate-revision: ## Create a new migration (MESSAGE="name")
	$(VENV)/bin/alembic revision --autogenerate -m "$(MESSAGE)"

lint: ## Lint and format-check
	$(VENV)/bin/ruff check app tests
	$(VENV)/bin/ruff format --check app tests

format: ## Auto-fix lint issues and format
	$(VENV)/bin/ruff check --fix app tests
	$(VENV)/bin/ruff format app tests

typecheck: ## Static type checking
	$(VENV)/bin/mypy app

test: ## Run all tests (requires infra: make up)
	$(VENV)/bin/pytest

test-unit: ## Run unit tests only (no infrastructure needed)
	$(VENV)/bin/pytest -m "not integration"

run: ## Run the API with hot reload
	$(VENV)/bin/uvicorn app.gateway.main:app --reload --host 0.0.0.0 --port 8000

executor-image: ## Build the sandbox executor image used by terminal tools
	docker build -t coding-agent-executor:latest -f infra/executor/Dockerfile infra/executor

clean: ## Remove venv and caches
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
