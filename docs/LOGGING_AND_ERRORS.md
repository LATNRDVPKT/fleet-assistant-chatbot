# Logs and error codes

## Reading the logs

Every line has: time · level · module · `req=<request_id>` · message. Pipeline steps are numbered `[01]`–`[14]`,
and sub-steps are drawn as a tree (`├─` / `└─`). Filter one request with `grep req=<id>`, or in CloudWatch
Logs Insights with `filter request_id = "<id>"`.

A real request (offline mode):

```text
api            | req=cb41576a6977 | [01] gateway ▶ POST /v1/chat from 10.0.1.23
api.security   | req=cb41576a6977 |   ├─ 1a auth ✓ tenant=demo-tenant
api.security   | req=cb41576a6977 |   ├─ 1b rate limit ✓
api            | req=cb41576a6977 |   ├─ 1c request valid ✓ vehicle=EV-60 chars=55
api            | req=cb41576a6977 |   └─ 1d capacity ✓ inflight=1/32
orchestrator   | req=cb41576a6977 | ▶ question received | tenant=demo-tenant vehicle=EV-60 q="What is the tyre pressure for the rear axle when laden?"
orchestrator   | req=cb41576a6977 | [02] vehicle_context ✓ 0.0 ms | vehicle=EV-60 region=India tenant=demo-tenant
preprocess     | req=cb41576a6977 |   ├─ 3a normalise        | "What is the tyre pressure for the rear axle when laden?"
preprocess     | req=cb41576a6977 |   ├─ 3b language         | en
preprocess     | req=cb41576a6977 |   ├─ 3c spell-correct    | fixes=none
preprocess     | req=cb41576a6977 |   └─ 3d safety check     | passed
orchestrator   | req=cb41576a6977 | [03] preprocess ✓ 6.7 ms | lang=en fixes=0 blocked=False
orchestrator   | req=cb41576a6977 | [04] understanding ✓ 0.9 ms | intent=tyres vehicles=EV-60 dtc=- conflict=False
orchestrator   | req=cb41576a6977 | [05] query_rewrite ✓ 0.0 ms | expansions=2 query_len=164
orchestrator   | req=cb41576a6977 | [06] retrieval_plan ✓ 0.0 ms | filters={'vehicle_model': ['EV-60', 'ALL'], 'region': 'India'} top_k=5 prefer=EV-60
hybrid         | req=cb41576a6977 |   ├─ 7a vector search      26.5 ms | hits=20 top=VM-EV60-OM::0005 0.6468
hybrid         | req=cb41576a6977 |   ├─ 7b BM25 search         0.9 ms | hits=20 top=VM-EV60-OM::0005 75.28
hybrid         | req=cb41576a6977 |   ├─ 7c RRF fusion          0.1 ms | candidates=20 found_by_both=13
hybrid         | req=cb41576a6977 |   └─ 7d rerank + boost      1.5 ms | reranker=lexical boosted=18 top=VM-EV60-OM::0005 score=1.0
orchestrator   | req=cb41576a6977 | [07] hybrid_retrieval ✓ 29.3 ms | vector_hits=20 bm25_hits=20 fused=20 ...
validator      | req=cb41576a6977 |   ├─ keep VM-EV60-OM::0005  score=1.0  table page=2 section="4. Tyre Specifications and Pressures"
validator      | req=cb41576a6977 |   ├─ keep VM-EV60-OM::0017  score=0.7  text  page=5 section="11. Energy Efficiency Guidance"
orchestrator   | req=cb41576a6977 | [08] context_validation ✓ 1.3 ms | kept=5 top_score=1.0 sufficient=True
orchestrator   | req=cb41576a6977 | [10] context_builder ✓ 0.0 ms | sources=5 chars=3109 docs=FLEET-HANDBOOK,VM-EV60-OM
orchestrator   | req=cb41576a6977 | [11] llm_generate ✓ 0.3 ms | model=extractive in_tok=0 out_tok=0
grounding      | req=cb41576a6977 |   ├─ claim ✓ cites=S2 support=1.00 | "Keep tyres at the laden pressure when carrying more than half payload."
grounding      | req=cb41576a6977 |   ├─ claim ✓ cites=S1 support=1.00 | "Rear axle — Cold pressure (unladen): 4.5 bar (65 psi); Cold pressure ("
orchestrator   | req=cb41576a6977 | [12] grounding_check ✓ 0.4 ms | grounded=True ratio=1.0 claims=2 unsupported=0
orchestrator   | req=cb41576a6977 | [14] final_response ✓ 0.0 ms | status=answered citations=2 latency_ms=39.5
api            | req=cb41576a6977 | [01] gateway ◀ POST /v1/chat → 200 | 42.5 ms
```

With Bedrock, step 11 shows the call, retries and fallback:

```text
llm | req=... |   ├─ LLM call attempt 1 → bedrock:anthropic.claude-haiku-...
llm | req=... |   ├─ FC-3002 LLM call attempt 1 failed: ThrottlingException: Too many requests
llm | req=... |   ├─ retrying in 0.43 s
llm | req=... |   ├─ LLM call attempt 2 → bedrock:anthropic.claude-haiku-...
llm | req=... |   └─ LLM call ✓ 1840.2 ms | in_tok=2711 out_tok=184
```

Set `LOG_LEVEL=DEBUG` to also see every dropped chunk and why. Set `LOG_FORMAT=json` on AWS, where each line becomes a
JSON object with fields `ts, level, logger, request_id, msg` (and `event`, `status`, `code`, `latency_ms`, ... on
the `chat_completed` audit line). Phone numbers and e-mail addresses are masked before logging.

## Error codes

| Code | Name | HTTP | Retry? | When |
| --- | --- | --- | --- | --- |
| FC-1001 | AUTH_MISSING | 401 | no | no `X-API-Key` header |
| FC-1002 | AUTH_INVALID | 401 | no | unknown key |
| FC-1004 | RATE_LIMITED | 429 | yes, after `Retry-After` | too many requests per minute for this key |
| FC-1005 | VALIDATION_FAILED | 422 | no | bad body, question too long, unknown vehicle (see `detail`) |
| FC-2001 | BLOCKED_UNSAFE | 200 `status=blocked` | no | tampering / falsification request |
| FC-2002 | BLOCKED_INJECTION | 200 `status=blocked` | no | tries to override the assistant's instructions |
| FC-2003 | UNSUPPORTED_LANGUAGE | 200 `status=blocked` | no | non-English script |
| FC-2004 | CLARIFICATION_NEEDED | 200 | — | model-dependent question without a vehicle |
| FC-2005 | NOT_FOUND | 200 `status=not_found` | no | not in the approved documents |
| FC-2006 | UNGROUNDED_REFUSED | 200 `status=not_found` | no | no verified answer after repair |
| FC-3001 | LLM_TIMEOUT | logged; answer degraded | — | LLM call > `LLM_TIMEOUT_SECONDS` (not retried) |
| FC-3002 | LLM_THROTTLED | logged; retried once | — | Bedrock throttling |
| FC-3003 | LLM_ERROR | logged; retried once | — | other LLM error |
| FC-3004 | CIRCUIT_OPEN | logged; answer degraded | — | LLM failed 5 times in a row → skipped for 30 s |
| FC-3006 | EMBEDDING_FAILED | logged; BM25 only | — | vector search failed |
| FC-3007 | RERANKER_FAILED | logged; RRF order | — | reranker failed |
| FC-5001 | INDEX_NOT_READY | 503 | yes | index missing at start-up |
| FC-5002 | INDEX_EMBEDDING_MISMATCH | 503 | no | index built with another embedding model → `ingest --rebuild` |
| FC-5003 | INTERNAL_ERROR | 500 | yes | unexpected error (stack trace in logs only) |
| FC-5004 | OVERLOADED | 503 | yes, after `Retry-After: 2` | too many requests in flight on this worker |

Error body (every non-200 response):

```json
{"error": {"code": "FC-1005", "name": "VALIDATION_FAILED", "message": "The request is not valid.",
           "retryable": false, "request_id": "6432c4cdef75",
           "detail": "vehicle_model: Value error, unknown vehicle_model 'ZX-9'. Known: ['HX-400', 'EV-60', 'CX-32']"}}
```

When a fallback was used, a 200 response carries `"degraded": "extractive_fallback"` (LLM failed),
`"bm25_only"` (vector search failed) or `"no_rerank"` (reranker failed).
