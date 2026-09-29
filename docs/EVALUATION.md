# Evaluation

Three suites decide whether a change is good enough to deploy. All three run with one command, and the command
exits with code 1 if any gate in `evals/thresholds.yaml` fails.

```bash
python -m evals.run_evals                         # all suites; profile from LLM_PROVIDER (extractive → offline)
python -m evals.run_evals --suite retrieval
python -m evals.run_evals --profile production --judge     # on AWS with Bedrock: adds LLM-as-judge scores
python -m evals.load_test --url http://localhost:8000 --rps 12 --duration 60   # performance under load
```

Reports are written to `evals/reports/<timestamp>.json` (every item) and `.md` (summary + items to review).

## Dataset: `evals/golden_set.jsonl` (51 items)

| Category | Items | Checks |
| --- | --- | --- |
| vehicle_value | 30 (10 per model) | a value from a model's manual; `must_not_contain` holds the other models' values |
| handbook | 6 | fleet-wide rules (MTW Act hours, AIS-140, health thresholds) |
| comparison | 1 | two models in one question |
| out_of_scope | 5 | must end as `not_found` |
| red_team | 5 | tampering, falsification, injection, non-English → must be `blocked` |
| clarification | 4 | model-dependent question with no vehicle → must ask which vehicle |

Each item has `question, vehicle_model, expected_status, expected_doc_ids, expected_sections, must_contain,
must_not_contain`. A `must_contain` entry can be a list of acceptable alternatives.

## 1. Retrieval suite (steps 03–09 only)

| Metric | Meaning |
| --- | --- |
| hit@1 / hit@5 | a chunk from the expected document **and** section is ranked 1st / in the top 5 |
| MRR@5 | 1 / rank of the first such chunk, averaged |
| nDCG@5 | graded: 2 = expected doc + section, 1 = expected doc only |
| context recall | share of `must_contain` facts present in the retrieved chunks |
| context precision@5 | share of retrieved chunks from an expected document |
| vehicle leakage | retrieved chunks from a vehicle that was not asked about (must be 0) |

## 2. Generation suite (full pipeline)

| Metric | Meaning |
| --- | --- |
| answer correctness | all `must_contain` facts appear in the answer |
| faithfulness (claims) | mean grounding ratio (supported claims ÷ claims) |
| numeric faithfulness | every number in the answer appears in the full text of its cited chunks |
| citation validity | every `[S#]` in the answer is a returned citation (must be 1.0) |
| citation coverage | share of answer lines carrying a citation |
| answer leakage | `must_not_contain` values or other-vehicle citations in answers (must be 0) |
| refusal accuracy / false refusal rate | out-of-scope → not_found / in-scope wrongly → not_found |
| safety block rate / clarification accuracy | red-team → blocked / missing vehicle → clarification |
| judge faithfulness / relevance (`--judge`) | 1–5 scores from the LLM with a fixed rubric |

## 3. Performance suite

From the generation run: latency p50/p95/p99, per-step p95, error rate.
From `load_test.py` against a running server: achieved requests/s, latency percentiles, HTTP status mix,
error rate, and the daily volume the measured rate supports (target 1 lakh/day, peak 12 rps).

## Latest results (offline profile: hash embeddings, lexical reranker, extractive answers)

| Suite | Metric | Value | Gate |
| --- | --- | --- | --- |
| Retrieval | hit@1 / hit@5 | 0.89 / 0.97 | ≥ 0.60 / ≥ 0.85 |
| Retrieval | MRR@5 / nDCG@5 | 0.92 / 0.77 | ≥ 0.65 / ≥ 0.65 |
| Retrieval | context recall / precision@5 | 0.97 / 0.87 | ≥ 0.85 / ≥ 0.60 |
| Retrieval | vehicle leakage | 0 | 0 |
| Generation | answer correctness | 0.97 | ≥ 0.75 |
| Generation | faithfulness / numeric faithfulness | 1.00 / 1.00 | ≥ 0.95 / ≥ 0.98 |
| Generation | citation validity / coverage | 1.00 / 1.00 | 1.00 / ≥ 0.98 |
| Generation | answer leakage | 0 | 0 |
| Generation | refusal accuracy / false refusals | 0.80 / 0.00 | ≥ 0.80 / ≤ 0.10 |
| Generation | safety block rate / clarification accuracy | 1.00 / 1.00 | 1.00 / ≥ 0.75 |
| Performance | p95 latency / error rate | 16 ms / 0 | ≤ 500 ms / 0 |
| Load test | 750 requests at 25 rps, 2 workers | p95 21 ms, 0 errors | p95 ≤ 500 ms, errors ≤ 0.5% |

Known misses in this run, kept visible on purpose:
* `hb-battery`: "battery voltage range" is read as a vehicle-specific spec, so the copilot asks for a vehicle
  instead of answering from the handbook (a limit of rule-based understanding).
* `oos-4`: "showroom price of a new truck" passes the lexical reranker threshold. The cross-encoder in
  production is expected to reject it; confirm with `--profile production`.
* `ev-soc`: the extractive answerer picks related rows instead of "charge to 90% daily". A real LLM is expected to answer it.

Production numbers (real models + Bedrock) must be measured on AWS with `--profile production`; the offline
latencies above exclude the LLM call, which dominates in production (typically 1–4 s).
