"""
Load test for a RUNNING API (performance suite, part 2).

  python -m evals.load_test --url http://localhost:8000 --rps 12 --duration 60
  python -m evals.load_test --url http://<ec2-ip> --rps 25 --duration 300 --api-key <key>

It sends requests at a fixed rate (like real users, who don't wait for each other), using a mix of
questions from the golden set, then reports latency percentiles, status codes, error rate and what the
measured rate means for daily volume (target: 1 lakh = 100,000 requests/day, peak 12 rps).

Tip: start the server with a high RATE_LIMIT_PER_MINUTE (e.g. 100000) so the test measures capacity,
not the per-key rate limit.
"""
import argparse
import asyncio
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

GOLDEN = Path(__file__).parent / "golden_set.jsonl"
TARGET_PER_DAY = 100_000
DESIGN_PEAK_RPS = 12


def pct(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    return round(v[min(len(v) - 1, math.ceil(p * len(v)) - 1)], 1)


async def one_request(client, url, key, item, results):
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{url}/v1/chat", headers={"X-API-Key": key},
                              json={"question": item["question"], "vehicle_model": item.get("vehicle_model")})
        ms = (time.perf_counter() - t0) * 1000
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        code = body.get("error", {}).get("code") if r.status_code != 200 else body.get("status")
        results.append({"http": r.status_code, "ms": ms, "code": code})
    except Exception as e:
        results.append({"http": 0, "ms": (time.perf_counter() - t0) * 1000, "code": type(e).__name__})


async def run(url, key, rps, duration, timeout):
    items = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    results, tasks = [], []
    total = int(rps * duration)
    print(f"▶ load test: {total} requests at {rps} rps for {duration}s against {url}")
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=100)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        start = time.perf_counter()
        for i in range(total):
            # open-loop schedule: request i starts at i / rps seconds, whether or not earlier ones finished
            delay = start + i / rps - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one_request(client, url, key, random.choice(items), results)))
            if (i + 1) % max(1, int(rps * 10)) == 0:
                done = len(results)
                print(f"  … sent {i + 1}/{total}, completed {done}, errors so far "
                      f"{sum(1 for r in results if r['http'] >= 500 or r['http'] == 0)}")
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start
    return results, elapsed


def report(results, elapsed, rps, max_p95_ms, max_error_rate):
    ok = [r["ms"] for r in results if r["http"] == 200]
    errors = [r for r in results if r["http"] >= 500 or r["http"] == 0]
    throttled = [r for r in results if r["http"] == 429]
    achieved = len(results) / elapsed
    error_rate = len(errors) / len(results) if results else 1
    p95 = pct(ok, 0.95)
    print("\n================ LOAD TEST RESULT ================")
    print(f"requests          {len(results)} in {elapsed:.1f}s  → achieved {achieved:.1f} rps (target {rps})")
    print(f"HTTP status       {dict(Counter(r['http'] for r in results))}")
    print(f"outcome codes     {dict(Counter(r['code'] for r in results))}")
    print(f"latency (200 OK)  p50={pct(ok, .5)} ms  p95={p95} ms  p99={pct(ok, .99)} ms  max={round(max(ok), 1) if ok else 0} ms")
    print(f"errors (5xx/net)  {len(errors)}  → error rate {error_rate:.2%}   rate-limited (429): {len(throttled)}")
    print("---------------- 1 lakh / day check ----------------")
    print(f"daily volume at this rate if sustained: {achieved * 86400:,.0f} requests/day (need {TARGET_PER_DAY:,})")
    print(f"design peak {DESIGN_PEAK_RPS} rps covered: {'YES' if achieved >= DESIGN_PEAK_RPS and error_rate <= max_error_rate else 'NO'}")
    passed = p95 <= max_p95_ms and error_rate <= max_error_rate
    print(f"gates: p95 <= {max_p95_ms} ms and error rate <= {max_error_rate:.2%} → {'PASS ✓' if passed else 'FAIL ✗'}")
    return passed


def main():
    ap = argparse.ArgumentParser(description="Fleet Copilot load test")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--api-key", default="dev-key-123")
    ap.add_argument("--rps", type=float, default=DESIGN_PEAK_RPS)
    ap.add_argument("--duration", type=int, default=60, help="seconds")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--max-p95-ms", type=float, default=6000)
    ap.add_argument("--max-error-rate", type=float, default=0.005)
    a = ap.parse_args()
    results, elapsed = asyncio.run(run(a.url.rstrip("/"), a.api_key, a.rps, a.duration, a.timeout))
    sys.exit(0 if report(results, elapsed, a.rps, a.max_p95_ms, a.max_error_rate) else 1)


if __name__ == "__main__":
    main()
