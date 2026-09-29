# Scaling to 1 lakh (100,000) requests per day

There is no answer cache, so every request runs the full pipeline. The numbers below are planning estimates;
confirm them with `evals/load_test.py` on your real instances.

## Traffic

| Quantity | How | Value |
| --- | --- | --- |
| Requests per day | requirement | 100,000 |
| Average over 24 h | 100,000 ÷ 86,400 s | 1.2 requests/s |
| Busiest hour | assume 12% of the day's traffic | 3.3 requests/s |
| Peak minute | 3× the busiest-hour average | 10 → **design for 12 requests/s** |
| Test target | 2× design peak | 25 requests/s |
| Requests needing the LLM | ~70% are answered (others: blocked / clarification / not found) | ≈ 8.4 LLM calls/s at peak |
| Requests in flight at peak | 12 requests/s × ~3 s each | ≈ 36 |

## Where the time and CPU go (per answered request)

| Part | Time | CPU on the instance |
| --- | --- | --- |
| Steps 01–06, 08, 10, 12 | < 20 ms | tiny |
| Embedding the query (MiniLM) | ~15 ms | yes |
| Cross-encoder rerank of 20 candidates | ~150–300 ms | **most of the CPU** |
| Bedrock LLM call | 1–4 s | none (waiting on the network) |

Estimated CPU per request is ~0.3 s. At 12 requests/s that is about 3.6 vCPU busy, so a box with 8 vCPU
(or two with 4) keeps CPU near 50%. Waiting for Bedrock costs no CPU: each worker runs requests in threads,
and `MAX_INFLIGHT_REQUESTS=32` per worker leaves plenty of room for the ~36 requests in flight.

## Recommended setups

| Setup | Workers | Capacity (estimate) | Notes |
| --- | --- | --- | --- |
| Demo: 1 × t3.large (2 vCPU, 8 GB) | 2 | ~4 requests/s | fine for testing, not for 1 lakh/day peaks |
| Single box: 1 × c6i.2xlarge (8 vCPU, 16 GB) | 6 | ~15–18 requests/s | covers 1 lakh/day; restart = short outage |
| **Production: 2 × c6i.xlarge (4 vCPU, 8 GB) + ALB, 2 AZs** | 3 each | ~15–18 requests/s total | survives one instance/AZ failure; add a 3rd instance to grow |

Memory: about 1 GB per worker (PyTorch + two small models + index). Keep `WORKERS × 1 GB` below ~70% of RAM.

## Amazon Bedrock quota (usually the real limit)

At the 12 requests/s peak there are ~8.4 LLM calls/s ≈ **500 requests/min**. Each call uses ~2,800 input tokens
(question + up to 9,000 characters of sources) and ~250 output tokens, which comes to **≈ 1.4 M input tokens/min**.
In **Service Quotas → Amazon Bedrock**, request at least **600 requests/min and 1.5 M tokens/min** for your model.
If the quota is lower, answers still work but more of them fall back to extractive mode (logged as FC-3002 /
FC-3004 with a `degraded` flag). You can also lower `MAX_CONTEXT_CHARS` to 6000 to cut input tokens by about 30%.

## Daily cost drivers

* **LLM tokens:** ≈ 70,000 calls/day × ~3,050 tokens. Multiply by your model's Bedrock prices; the `InputTokens` /
  `OutputTokens` metrics in CloudWatch show the real numbers.
* **EC2:** instance hours (2 × c6i.xlarge or 1 × c6i.2xlarge), plus the ALB if used.
* **CloudWatch Logs:** ~1 GB/day at INFO level. Set a retention period (30–90 days).

## Load test checklist

```bash
# on each instance: RATE_LIMIT_PER_MINUTE=100000 in .env for the test, then restart
python -m evals.load_test --url https://<domain> --api-key <key> --rps 12 --duration 600   # peak, 10 min
python -m evals.load_test --url https://<domain> --api-key <key> --rps 25 --duration 300   # 2× peak
```

Pass = p95 ≤ 6 s and error rate ≤ 0.5%. While it runs, watch EC2 CPU (< 70%), the `Latency` metric,
and FC-3002 (Bedrock throttling) in the logs.
