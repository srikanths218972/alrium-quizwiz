.PHONY: help setup db-up db-down db-status migrate backend frontend dev test test-backend test-web test-postman \
        test-pgmove test-core-collection lint db-shell setup-sql test-setup-sql collection-integration backup postgres-preflight backup-docker-postgres restore-to-local-postgres \
        verify-postgres-migration migrate-docker-to-local collection openapi

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t 28

setup: ## create the Python virtual environment and install Python and frontend dependencies
	cd backend && uv sync --frozen
	npm ci

db-up: ## start only the Docker PostgreSQL container
	scripts/db up
db-down: ## stop the container (data volume kept)
	scripts/db down
db-status: ## container status and migrations of DATABASE_URL
	scripts/db status
migrate: ## apply migrations to the database named by DATABASE_URL
	scripts/db migrate
db-shell: ## psql session on DATABASE_URL
	scripts/db shell
backup: ## pg_dump of DATABASE_URL with checksum and manifest (local pg_dump)
	scripts/db backup

backend: ## start FastAPI locally (http://127.0.0.1:8000); prints the sign-in mode (entra or email) first
	scripts/backend
frontend: ## start Vite locally (http://localhost:5173)
	scripts/frontend
dev: ## start backend and frontend together; Ctrl+C stops both
	scripts/dev

test: lint test-backend test-web test-pgmove test-setup-sql test-core-collection test-postman ## every check, each against its own disposable database
lint: ## ruff, mypy, TypeScript
	cd backend && uv run --frozen ruff check qa_api tests && uv run --frozen mypy
	npm run typecheck
test-backend: ## FastAPI, database and migration tests against a disposable PostgreSQL
	scripts/test-db bash -c 'cd backend && uv run --frozen pytest'
test-web: ## frontend build and UI tests
	npm run build && npm run test:web
test-core-collection: ## the client Postman collection end to end, with SQL, outage and restart checks
	scripts/test-core-collection
test-postman: ## run the integration Postman collection with Newman against a disposable backend + database
	scripts/test-postman
test-pgmove: ## Docker -> local migration tool, isolated source and target
	scripts/test-pgmove

postgres-preflight: ## check tools, versions, connectivity and privileges for the move
	scripts/pg preflight
backup-docker-postgres: ## timestamped backup of SOURCE_DATABASE_URL
	scripts/pg backup
restore-to-local-postgres: ## restore BACKUP=<dir> into the new TARGET_DATABASE
	scripts/pg restore "$(BACKUP)"
verify-postgres-migration: ## compare source and target and run read-only checks
	scripts/pg verify
migrate-docker-to-local: ## preflight + backup + restore + verify (does not switch DATABASE_URL)
	scripts/pg migrate

collection: ## regenerate the Postman collection and environment (postman/)
	node postman/build.mjs
setup-sql: ## regenerate db/setup.sql (whole schema for a new database, for psql) from db/migrations
	scripts/build-setup-sql
test-setup-sql: ## check db/setup.sql with real psql in a throwaway PostgreSQL 18 container
	scripts/test-setup-sql
collection-integration: ## regenerate the integration regression suite (tests/collections/integration)
	node tests/collections/build-integration.mjs
openapi: ## export docs/openapi.json
	cd backend && uv run --frozen python -m qa_api.export_openapi ../docs/openapi.json
