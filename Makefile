# Common tasks. Needs `make` (native on macOS/Linux/CI; on Windows use Git Bash or WSL,
# or just copy the command you need). Nothing here is load-bearing -- every recipe is a
# one-liner you can run by hand.

PY ?= python
VERSION := $(shell grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
TEST_DATABASE_URL ?= postgresql://postgres:postgres@localhost:5432/sect_test

.DEFAULT_GOAL := help
.PHONY: help install pg test lint fmt check migrate status run tag clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Editable install with all extras
	$(PY) -m pip install -e ".[core,cli,dev]"

pg: ## Start a throwaway Postgres for the test suite
	docker run -d --name sect-pg -e POSTGRES_PASSWORD=postgres \
		-e POSTGRES_DB=sect_test -p 5432:5432 postgres:16-alpine

test: ## Run the test suite (needs Postgres; see `make pg`)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) -m pytest -q

lint: ## ruff check
	$(PY) -m ruff check .

fmt: ## ruff format
	$(PY) -m ruff format .

check: ## What CI runs: lint + format check + tests
	$(PY) -m ruff check . && $(PY) -m ruff format --check . && \
		TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) -m pytest -q

migrate: ## Apply pending migrations (reads DATABASE_URL)
	$(PY) -m sect.core.db migrate

status: ## Show migration status (reads DATABASE_URL)
	$(PY) -m sect.core.db status

run: ## Run the server on :8000
	$(PY) -m uvicorn sect.core.app:create_app --factory --reload --port 8000

tag: ## Tag the current commit as v$(VERSION)
	git tag -a "v$(VERSION)" -m "the-sect v$(VERSION)" && \
		echo "tagged v$(VERSION) -- push with: git push origin v$(VERSION)"

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache dist build **/__pycache__ *.egg-info src/*.egg-info
