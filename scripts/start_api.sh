#!/usr/bin/env bash
# =====================================================================
# Container entrypoint for the API.
#   1. show configuration   2. make sure the search index exists (build it if not)
#   3. print index stats    4. start FastAPI (uvicorn) with N worker processes
# =====================================================================
set -euo pipefail
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | INFO  | start_api              | req=- | $*"; }

WORKERS="${WORKERS:-2}"
INDEX_DIR="${INDEX_DIR:-/app/data/index}"

log "[1/4] config: LLM_PROVIDER=${LLM_PROVIDER:-extractive} EMBEDDING_PROVIDER=${EMBEDDING_PROVIDER:-local} RERANKER=${RERANKER:-cross-encoder} WORKERS=${WORKERS}"

log "[2/4] checking search index in ${INDEX_DIR}"
if [ ! -f "${INDEX_DIR}/index_meta.json" ]; then
  log "      no index yet → building it from config/manifest.yaml (first start takes ~1 minute)"
  python -m app.ingestion.cli ingest --rebuild
else
  log "      index found → re-checking for changed documents (unchanged ones are skipped)"
  if ! python -m app.ingestion.cli ingest; then
    log "      incremental ingest failed (usually a different embedding model) → full rebuild"
    python -m app.ingestion.cli ingest --rebuild
  fi
fi

log "[3/4] index summary:"
python -m app.ingestion.cli stats

log "[4/4] starting FastAPI on 0.0.0.0:8000 with ${WORKERS} worker(s)"
exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS}" --timeout-keep-alive 30 --no-access-log
