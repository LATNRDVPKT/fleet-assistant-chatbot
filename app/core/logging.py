"""
Logging with a request id on every line and a timer for every pipeline step.

Text format (local):
  2026-09-28 12:00:01 | INFO  | pipeline.preprocess | req=3f2a1c | [03] preprocess ✓ 1.2 ms | lang=en fixes=1
JSON format (AWS / CloudWatch Logs Insights):
  {"ts": "...", "level": "INFO", "logger": "...", "request_id": "3f2a1c", "msg": "...", ...}
"""
import contextvars
import json
import logging
import sys
import time
from contextlib import contextmanager

from app.core.config import get_settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if s.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-5s | %(name)-22s | req=%(request_id)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(s.log_level)
    for noisy in ("httpx", "urllib3", "chromadb", "sentence_transformers", "botocore", "pdfminer", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


class Trace:
    """Collects the timing + summary of each pipeline step for one request."""

    def __init__(self):
        self.steps: list[dict] = []

    @contextmanager
    def step(self, number: int, name: str, logger: logging.Logger):
        info: dict = {}
        start = time.perf_counter()
        logger.debug(f"[{number:02d}] {name} ▶ start")
        try:
            yield info                      # the step fills `info` with a short summary
            status = "✓"
        except Exception:
            status = "✗"
            raise
        finally:
            ms = round((time.perf_counter() - start) * 1000, 1)
            summary = " ".join(f"{k}={v}" for k, v in info.items())
            logger.info(f"[{number:02d}] {name} {status} {ms} ms | {summary}")
            self.steps.append({"step": number, "name": name, "status": status, "ms": ms, **info})

    def total_ms(self) -> float:
        return round(sum(s["ms"] for s in self.steps), 1)
