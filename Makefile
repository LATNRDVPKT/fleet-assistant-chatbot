# Handy shortcuts. On Windows, run the command shown after each target instead.
# Offline mode (no downloads, no API keys): prefix with OFFLINE=1, e.g.  make OFFLINE=1 api

ifdef OFFLINE
export EMBEDDING_PROVIDER=hash
export RERANKER=lexical
export LLM_PROVIDER=extractive
endif

install:        ## install everything for local development
	pip install -r requirements-dev.txt

validate:       ## check config/manifest.yaml and the document files
	python -m app.ingestion.cli validate

ingest:         ## build/update the search index (only changed documents)
	python -m app.ingestion.cli ingest

rebuild:        ## rebuild the whole index from scratch
	python -m app.ingestion.cli ingest --rebuild

inspect:        ## show the chunks of one document: make inspect DOC=VM-EV60-OM
	python -m app.ingestion.cli inspect --doc $(DOC)

api:            ## run the FastAPI server with auto-reload (http://localhost:8000/docs)
	uvicorn app.api.main:app --reload --port 8000

ui:             ## run the Streamlit UI (http://localhost:8501)
	streamlit run ui/streamlit_app.py

test:           ## run the automated tests
	pytest -q tests

eval:           ## run retrieval + generation + performance evaluations
	python -m evals.run_evals

loadtest:       ## load test a running API: make loadtest RPS=12 SECONDS=60
	python -m evals.load_test --rps $(or $(RPS),12) --duration $(or $(SECONDS),60)

docker-up:      ## run api + ui + nginx with Docker Compose on http://localhost
	docker compose up -d --build

docker-logs:    ## follow the API logs
	docker compose logs -f api

docker-down:
	docker compose down

.PHONY: install validate ingest rebuild inspect api ui test eval loadtest docker-up docker-logs docker-down
