"""
Step 01 (part) — API key authentication and per-key rate limiting.

* API keys come from the API_KEYS setting: "key1:tenantA,key2:tenantB".
  On AWS they are stored in SSM Parameter Store / Secrets Manager and passed as an env variable.
* Rate limiting is a sliding 60-second window per API key, kept in this process's memory.
  (Each worker process keeps its own window, so the real limit is RATE_LIMIT_PER_MINUTE × workers.)
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import Header

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger("api.security")


def _key_map() -> dict[str, str]:
    out = {}
    for pair in get_settings().api_keys.split(","):
        if ":" in pair:
            key, tenant = pair.strip().split(":", 1)
            out[key.strip()] = tenant.strip()
    return out


class RateLimiter:
    """At most N requests in any 60-second window, per API key."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> int | None:
        """Returns None if allowed, otherwise the seconds to wait."""
        limit = get_settings().rate_limit_per_minute
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:           # forget requests older than 60 s
                q.popleft()
            if len(q) >= limit:
                return int(60 - (now - q[0])) + 1
            q.append(now)
            return None


limiter = RateLimiter()


def authenticate(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict:
    """FastAPI dependency used by every protected endpoint."""
    if not x_api_key:
        log.warning("  ├─ 1a auth ✗ FC-1001 missing X-API-Key")
        raise AppError("FC-1001")
    tenant = _key_map().get(x_api_key)
    if not tenant:
        log.warning(f"  ├─ 1a auth ✗ FC-1002 unknown key ending '…{x_api_key[-4:]}'")
        raise AppError("FC-1002")
    log.info(f"  ├─ 1a auth ✓ tenant={tenant}")
    wait = limiter.check(x_api_key)
    if wait is not None:
        log.warning(f"  ├─ 1b rate limit ✗ FC-1004 tenant={tenant} retry_after={wait}s")
        raise AppError("FC-1004", retry_after=wait)
    log.info(f"  ├─ 1b rate limit ✓ API Key authentication successful for tenant {tenant} and key {x_api_key}")

    return {"tenant": tenant, "key": x_api_key}
