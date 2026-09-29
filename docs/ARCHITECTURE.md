# Architecture

The copilot answers a question in **14 numbered steps**. Each step is one small module, writes one log line
(plus sub-step lines), and has a defined behaviour when it fails.
**Fail closed** means an error blocks or refuses the request; **fail open** means we continue with a safe default and log a WARN.

| # | Step | Module | What it does | If it fails |
| --- | --- | --- | --- | --- |
| 01 | Gateway | `api/main.py`, `api/security.py` | 1a API key → tenant, 1b rate limit, 1c schema validation, 1d capacity check | 401 FC-1001/1002, 429 FC-1004, 422 FC-1005, 503 FC-5004 |
| 02 | Vehicle context | `pipeline/orchestrator.py` | resolve selected vehicle (aliases), region, tenant | unknown model already rejected at 01 |
| 03 | Preprocess | `pipeline/preprocess.py` | 3a normalise text/units, 3b language, 3c domain spell-fix, 3d safety & injection check | spell-fix fails open; **safety check fails closed** |
| 04 | Understanding | `pipeline/understanding.py` | intent (13 types), DTC codes, vehicle/variant in the question, clarification rule | model-dependent question with no vehicle → `clarification_needed` FC-2004 |
| 05 | Rewrite | `pipeline/rewriter.py` | glossary synonyms + section hint + vehicle name | — |
| 06 | Plan | `pipeline/planner.py` | filters `vehicle_model ∈ {model, ALL}` + region, top-k, preferred vehicle | — |
| 07 | Hybrid retrieval | `retrieval/hybrid.py` | 7a vector top 20 · 7b BM25 top 20 · 7c RRF (k=60) · 7d rerank + 0.10 own-manual boost | vector fails → BM25 only (FC-3006); reranker fails → RRF order (FC-3007) |
| 08 | Validation | `pipeline/validator.py` | drop other-vehicle, missing-metadata, low-score (< 0.20), duplicate chunks | error → treated as insufficient (fail closed) |
| 09 | Recovery | `pipeline/orchestrator.py` | ≤ 2 more attempts: keyword-only query, then LLM-rewritten query | still insufficient → `not_found` FC-2005 |
| 10 | Context builder | `pipeline/context_builder.py` | number sources S1..Sn with title, version, vehicle, section, page; 9,000-char budget | — |
| 11 | LLM | `pipeline/llm.py` | Bedrock / OpenAI / Ollama / extractive; 20 s timeout, 1 retry, circuit breaker | falls back to extractive answer (still cited), `degraded` flag |
| 12 | Grounding | `pipeline/grounding.py` | per claim: valid citation, ≥ 60% word support, every number in the cited chunk | error → ungrounded (fail closed) |
| 13 | Repair | `pipeline/orchestrator.py` | regenerate with feedback → re-retrieve → keep verified sentences → refuse | `not_found` FC-2006 |
| 14 | Response | `pipeline/orchestrator.py` | answer, citations, status, code, confidence, latency, trace; audit log + metrics | — |

## Key design decisions

* **Vehicle isolation by construction.** The retrieval filter only allows the asked vehicle's manual plus the
  fleet handbook (`ALL`), and the validator checks again. The HX-400 oil interval can never be served for the EV-60.
* **Vehicle manual beats handbook.** A +0.10 score boost for the vehicle's own manual, and the prompt tells the
  LLM to prefer it when the two differ.
* **Hybrid search.** Manuals are full of exact tokens (P2463, 0.30 g, HX-400) that vectors blur, and of
  paraphrases that keywords miss, so the pipeline uses both.
* **Tables stay whole.** Rows are never split; the header repeats in every chunk of a big table; each table is stored as
  Markdown (for the LLM/UI) and as one line per row (for search).
* **Numbers are checked.** A number in the answer that is not in its cited chunk makes the claim unsupported.
* **Degrade, don't fail.** Every dependency has a fallback, and the response says when one was used.
* **No answer cache.** Every question runs the full pipeline, so answers always reflect the current index.

## Runtime layout

```
nginx :80 ──► /            ui   (Streamlit, calls the API over HTTP)
          └─► /v1 /health  api  (uvicorn, WORKERS processes; each loads the index + models once at start-up)
api ──► Amazon Bedrock (IAM role)          logs (stdout) ──► CloudWatch Logs + EMF metrics
```
