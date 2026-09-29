# 🚚 Fleet Assistance Copilot

A RAG chatbot that answers **vehicle-specific** fleet questions from approved manuals and the fleet handbook.
Every answer is cited to a document, section and page, and a wrong number never reaches the user.

* **FastAPI** backend, a **Streamlit** UI, and **Docker Compose** deployment on **AWS EC2** (manual guide included)
* **Structure-aware ingestion** of PDF and Word documents with **table-aware chunking**
* **Hybrid retrieval**: vector + BM25 → RRF → cross-encoder rerank, with **hard vehicle isolation**
* **Grounding check** on every answer: claim ↔ source, citation validity, and every number present in its source
* **Clear logs for every step** (one `request_id` per question) and **standard error codes** (FC-xxxx)
* **Evaluations**: retrieval, generation and performance suites with pass/fail gates, plus a load tester
* **No answer cache**: every question runs the full pipeline

```
 question ─► [01] gateway (key, rate limit, validation, capacity)
          ─► [02] vehicle context ─► [03] preprocess (normalise, language, spelling, safety)
          ─► [04] understanding (intent, DTC codes, vehicle) ─► [05] query rewrite ─► [06] retrieval plan
          ─► [07] hybrid retrieval: 7a vector │ 7b BM25 │ 7c RRF │ 7d rerank + own-manual boost
          ─► [08] context validation ──insufficient──► [09] recovery (retry ≤ 2) ──► not_found
          ─► [10] context builder [S1..Sn] ─► [11] LLM (timeout, retry, circuit breaker, fallback)
          ─► [12] grounding check ──not grounded──► [13] regenerate / re-retrieve / trim / refuse
          ─► [14] final response: answer + citations (doc, version, section, page) + status + code
```

## Quick start (5 minutes, fully offline, no API keys)

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# offline mode: no model downloads, no LLM (answers quote the manuals)
export EMBEDDING_PROVIDER=hash RERANKER=lexical LLM_PROVIDER=extractive   # Windows: set VAR=value

python -m app.ingestion.cli ingest          # 1. build the search index (≈10 s, logs every step)
uvicorn app.api.main:app --port 8000        # 2. start the API  →  http://localhost:8000/docs
streamlit run ui/streamlit_app.py           # 3. in a 2nd terminal: the UI → http://localhost:8501
```

Ask *"What is the harsh braking threshold?"* with vehicle **EV-60**, then **CX-32**: the answers differ
(0.40 g vs 0.25 g) and each cites its own manual and page.

**Full quality mode** (real embedding model + reranker + Claude on Bedrock): copy `.env.example` to `.env`,
set `EMBEDDING_PROVIDER=local`, `RERANKER=cross-encoder`, `LLM_PROVIDER=bedrock` (AWS credentials needed), then
run `python -m app.ingestion.cli ingest --rebuild` (the embedding model changed) and start the API.

**Windows note:** chromadb's default Rust bindings crash with an access violation on native Windows
(upstream bug, [chroma-core/chroma#6052](https://github.com/chroma-core/chroma/issues/6052)). `app/retrieval/stores.py`
works around this by forcing chromadb's legacy `SegmentAPI` on `win32`, which needs the `chroma-hnswlib` C
extension. It has no Windows wheel on PyPI, so fetch the real conda-forge build with:
```bash
pip install zstandard
python scripts/install_windows_hnswlib.py
```
Not needed on Linux/Docker, where the default Rust bindings work fine.

## Try the API

```bash
curl -s -X POST localhost:8000/v1/chat -H "X-API-Key: dev-key-123" -H "Content-Type: application/json" \
     -d '{"question":"How often should I change the engine oil?","vehicle_model":"HX-400","debug":true}'
```

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/chat` | ask a question (`question`, optional `vehicle_model`, `variant`, `region`, `debug`) |
| `POST /v1/feedback` | thumbs up/down for a `request_id` |
| `GET /v1/vehicles` | vehicle list for the UI |
| `GET /health` | readiness (index version, chunk count, providers) |
| `GET /docs` | interactive Swagger UI |

Response `status` is one of `answered`, `clarification_needed` (FC-2004), `not_found` (FC-2005/2006), `blocked`
(FC-2001/2002/2003). Errors always look like
`{"error": {"code": "FC-1004", "name": "RATE_LIMITED", "message": "...", "retryable": true, "request_id": "..."}}`.
All codes: [`docs/LOGGING_AND_ERRORS.md`](docs/LOGGING_AND_ERRORS.md).

## Tests and evaluations

```bash
pytest -q tests                                  # 38 tests: ingestion, pipeline, grounding, LLM resilience, API
python -m evals.run_evals                        # retrieval + generation + performance gates (exit 1 on failure)
python -m evals.load_test --rps 12 --duration 60 # against a running API: latency, errors, 1 lakh/day check
```

Latest offline run: hit@5 0.97, MRR 0.92, answer correctness 0.97, faithfulness 1.00, citation validity 1.00,
vehicle leakage 0, safety block rate 1.00, p95 16 ms. Load test: 750 requests at 25 rps, 0 errors, p95 21 ms.
See [`docs/EVALUATION.md`](docs/EVALUATION.md).

## Project layout

```
app/
  api/          main.py (FastAPI, error handlers, capacity guard) · security.py (keys, rate limit) · schemas.py
  core/         config.py (all settings) · logging.py (step logs + timer) · errors.py (FC codes) · metrics.py · text.py · registry.py
  ingestion/    loaders.py (PDF/DOCX → ordered elements) · chunker.py (text + table chunks) · indexer.py · cli.py
  retrieval/    embeddings.py · stores.py (Chroma + BM25) · reranker.py · hybrid.py (7a–7d)
  pipeline/     orchestrator.py (the 14 steps) · preprocess · understanding · rewriter · planner · validator ·
                context_builder · prompts · llm.py (providers + resilience) · grounding.py
ui/             streamlit_app.py
config/         manifest.yaml (documents) · vehicles.yaml (models) · glossary.yaml (synonyms, safety patterns)
data/           documents/ (ingested files) · source_docx/ (editable Word versions of the manuals) · index/ (generated)
evals/          golden_set.jsonl · thresholds.yaml · run_evals.py · load_test.py · reports/
tests/          pytest suite
deploy/         nginx.conf · aws/iam-policy.json · aws/ec2-user-data.sh
docs/           DEPLOY_AWS_MANUAL.md · ARCHITECTURE.md · INGESTION_GUIDE.md · EVALUATION.md · LOGGING_AND_ERRORS.md · SCALING.md
Dockerfile · Dockerfile.ui · docker-compose.yml · docker-compose.aws.yml · Makefile · .env.example
```

## Documents in this project

| doc_id | Document | Vehicle | Format |
| --- | --- | --- | --- |
| FLEET-HANDBOOK | Fleet Operations & Driver Safety Handbook | all vehicles | DOCX |
| VM-HX400-OM | Hexa HX-400 Heavy Haul Tractor manual | HX-400 | PDF (5 pages, 10 tables) |
| VM-EV60-OM | Voltra EV-60 Electric Delivery Van manual | EV-60 | PDF (6 pages, 12 tables) |
| VM-CX32-OM | Coachline CX-32 Intercity Bus manual | CX-32 | PDF (5 pages, 10 tables) |

The three vehicle manuals are **fictional sample documents** with realistic values, made to demonstrate
multi-model retrieval. Replace them with approved OEM manuals before real use. To add a model, see
[`docs/INGESTION_GUIDE.md`](docs/INGESTION_GUIDE.md).

## Deploy to AWS

Follow [`docs/DEPLOY_AWS_MANUAL.md`](docs/DEPLOY_AWS_MANUAL.md): Bedrock access, an IAM role, a security group,
EC2 with Docker Compose, CloudWatch logs, metrics and alarms, HTTPS, and scaling to 1 lakh requests/day.
