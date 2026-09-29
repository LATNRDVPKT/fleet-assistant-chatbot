"""
Metrics.

* In-memory counters → exposed at GET /metrics (handy locally).
* CloudWatch Embedded Metric Format (EMF) → when EMIT_EMF_METRICS=true, one JSON
  line per request is printed to stdout. CloudWatch Logs automatically turns
  these lines into CloudWatch metrics (no SDK calls, no extra cost per call).
"""
import json
import sys
import threading
import time
from collections import defaultdict

from app.core.config import get_settings

_lock = threading.Lock()
_counters: dict[str, float] = defaultdict(float)
_latencies: list[float] = []


def incr(name: str, value: float = 1.0) -> None:
    with _lock:
        _counters[name] += value


def observe_latency(ms: float) -> None:
    with _lock:
        _latencies.append(ms)
        if len(_latencies) > 5000:
            del _latencies[:1000]


def snapshot() -> dict:
    with _lock:
        lat = sorted(_latencies)
    pct = lambda p: round(lat[min(len(lat) - 1, int(len(lat) * p))], 1) if lat else 0.0
    return {"counters": dict(_counters), "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "count": len(lat)}}


def emit_emf(metrics: dict[str, float], dimensions: dict[str, str], units: dict[str, str] | None = None) -> None:
    """Print one CloudWatch EMF record. `metrics` = {"Latency": 812.0, "Grounded": 1, ...}"""
    s = get_settings()
    if not s.emit_emf_metrics:
        return
    units = units or {}
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": s.metrics_namespace,
                "Dimensions": [["Environment"], list(dimensions.keys())] if len(dimensions) > 1 else [list(dimensions.keys())],
                "Metrics": [{"Name": k, "Unit": units.get(k, "Count")} for k in metrics],
            }],
        },
        **dimensions,
        **metrics,
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()
