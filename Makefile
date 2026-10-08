PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: install run test test-postgres lint format demo docker-up docker-down clean

install:            ## Create a virtualenv and install app + dev dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

run:                ## Start the API with auto-reload on http://localhost:8000
	$(BIN)/uvicorn app.main:app --reload

test:               ## Run the test suite (SQLite)
	$(BIN)/pytest

test-postgres:      ## Run the test suite against a throwaway PostgreSQL container
	docker run -d --rm --name certgen-test-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=certs -p 55432:5432 postgres:16-alpine
	until docker exec certgen-test-db pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done; sleep 1
	TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/certs $(BIN)/pytest; \
	status=$$?; docker stop certgen-test-db >/dev/null; exit $$status

lint:               ## Lint and check formatting
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:             ## Auto-format the code
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

demo:               ## Submit a sample job to a running server and download the ZIP
	$(BIN)/python scripts/demo.py

docker-up:          ## Run API + PostgreSQL with Docker Compose
	docker compose up --build

docker-down:        ## Stop Docker Compose and delete its volumes
	docker compose down -v

clean:              ## Delete local runtime data (SQLite DB + generated PDFs)
	rm -rf data
