"""
Step 11 — LLM generation.

Providers (LLM_PROVIDER):
  bedrock     → Amazon Bedrock Converse API (Claude). Recommended on AWS; auth via the EC2 IAM role.
  openai      → OpenAI Chat Completions (OPENAI_API_KEY).
  ollama      → local model via Ollama (OLLAMA_URL).
  extractive  → no LLM: picks the best matching table rows / sentences from the sources and cites them.
                Zero cost, deterministic, fully offline. Used for tests, evals and as the safe fallback.

Resilience (ResilientLLM wraps every generative provider):
  * each call has a timeout (LLM_TIMEOUT_SECONDS)
  * throttling / server errors are retried once with a random (jittered) wait
  * after BREAKER_FAILURES failures in a row the circuit breaker "opens": for BREAKER_OPEN_SECONDS
    we stop calling the LLM and go straight to the fallback, then try one test call
  * fallback = extractive answer (quotes the sources, still cited) and the response is flagged degraded
"""
import random
import re
import threading
import time
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.registry import vehicles
from app.core.text import content_tokens, split_sentences
from app.pipeline.prompts import NOT_FOUND, QUERY_REWRITE_PROMPT, SYSTEM_PROMPT, USER_TEMPLATE

log = get_logger("pipeline.llm")


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    degraded: str | None = None        # "extractive_fallback" when the LLM failed and we quoted sources
    error_code: str | None = None      # FC-300x code of the failure that caused the fallback


# =============================================================== providers
class BaseLLM:
    name = "base"
    is_generative = True

    def complete(self, system: str, user: str) -> LLMResult:
        raise NotImplementedError

    def answer(self, question: str, vehicle: str, context: str, sources: list[dict], feedback: str = "") -> LLMResult:
        user = USER_TEMPLATE.format(vehicle=vehicle, question=question, context=context, feedback=feedback)
        t = time.perf_counter()
        result = self.complete(SYSTEM_PROMPT, user)
        result.latency_ms = round((time.perf_counter() - t) * 1000, 1)
        return result

    def rewrite_query(self, question: str) -> str | None:
        return self.complete("You rewrite search queries.", QUERY_REWRITE_PROMPT.format(question=question)).text.strip()


class BedrockLLM(BaseLLM):
    def __init__(self):
        import boto3
        from botocore.config import Config
        s = get_settings()
        # We do our own retries (see ResilientLLM), so boto3's are switched off.
        cfg = Config(read_timeout=s.llm_timeout_seconds, connect_timeout=5, retries={"max_attempts": 1})
        self.client = boto3.client("bedrock-runtime", region_name=s.aws_region, config=cfg)
        self.model_id = s.bedrock_model_id
        self.name = f"bedrock:{self.model_id}"

    def complete(self, system, user):
        s = get_settings()
        resp = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": s.llm_max_tokens, "temperature": s.llm_temperature},
        )
        usage = resp.get("usage", {})
        return LLMResult(resp["output"]["message"]["content"][0]["text"], self.name,
                         usage.get("inputTokens", 0), usage.get("outputTokens", 0))


class OpenAILLM(BaseLLM):
    def __init__(self):
        from openai import OpenAI
        s = get_settings()
        self.client = OpenAI(api_key=s.openai_api_key or None, timeout=s.llm_timeout_seconds, max_retries=0)
        self.model = s.openai_model
        self.name = f"openai:{self.model}"

    def complete(self, system, user):
        s = get_settings()
        resp = self.client.chat.completions.create(
            model=self.model, temperature=s.llm_temperature, max_tokens=s.llm_max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        u = resp.usage
        return LLMResult(resp.choices[0].message.content, self.name,
                         getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))


class OllamaLLM(BaseLLM):
    def __init__(self):
        s = get_settings()
        self.url, self.model = s.ollama_url.rstrip("/"), s.ollama_model
        self.name = f"ollama:{self.model}"

    def complete(self, system, user):
        import requests
        s = get_settings()
        r = requests.post(f"{self.url}/api/chat", timeout=s.llm_timeout_seconds, json={
            "model": self.model, "stream": False, "options": {"temperature": s.llm_temperature},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        data = r.json()
        return LLMResult(data["message"]["content"], self.name,
                         data.get("prompt_eval_count", 0), data.get("eval_count", 0))


class ExtractiveLLM(BaseLLM):
    """Answers by quoting the most relevant table rows / sentences, each with its [S#] citation."""
    name = "extractive"
    is_generative = False

    def __init__(self):
        self._vehicle_words = {w for v in vehicles() for a in v["aliases"] + [v["model"]] for w in content_tokens(a)}

    def answer(self, question, vehicle, context, sources, feedback=""):
        t = time.perf_counter()
        q = set(content_tokens(question)) - self._vehicle_words
        candidates = []                                   # (score, line)
        for src in sources[:3]:
            sid = src["id"]
            if src["content_type"] == "table":
                rows = [r for r in src["text"].splitlines() if r.startswith("|")]
                if len(rows) < 3:
                    continue
                header = [h.strip() for h in rows[0].strip("|").split("|")]
                header_hits = len(q & set(content_tokens(" ".join(header))))   # e.g. "Cold pressure (laden)"
                for row in rows[2:]:
                    cells = [c.strip() for c in row.strip("|").split("|")]
                    row_hits = len(q & set(content_tokens(" ".join(cells))))
                    if row_hits:                          # the row itself must match the question
                        detail = "; ".join(f"{h}: {v}" for h, v in zip(header[1:], cells[1:]) if v)
                        candidates.append((row_hits + header_hits + 0.5, f"{cells[0]} — {detail} [{sid}]"))
            else:
                for sent in re.split(r"\n+", src["text"]):
                    for s in split_sentences(sent.lstrip("-• ")):
                        s = re.sub(r"^A:\s*", "", s)
                        if s.endswith("?") or s.startswith("Q:"):       # skip FAQ questions
                            continue
                        score = len(q & set(content_tokens(s)))
                        if score:
                            candidates.append((score, f"{s.rstrip()} [{sid}]"))
        if not candidates:
            return LLMResult(NOT_FOUND, self.name, latency_ms=round((time.perf_counter() - t) * 1000, 1))
        best = max(c[0] for c in candidates)
        lines = [line for score, line in sorted(candidates, key=lambda c: -c[0]) if score >= best * 0.6]
        lines = list(dict.fromkeys(lines))[:4]
        text = "\n".join(f"- {line}" for line in lines)
        return LLMResult(text, self.name, latency_ms=round((time.perf_counter() - t) * 1000, 1))

    def rewrite_query(self, question):
        return None


# =============================================================== resilience
def classify_error(e: Exception) -> tuple[str, bool]:
    """Map any provider exception to (FC code, should_retry)."""
    text = f"{e.__class__.__name__} {e}".lower()
    if "timeout" in text or "timed out" in text:
        return "FC-3001", False                  # a timeout already cost 20 s; don't wait another 20 s
    if "throttl" in text or "429" in text or "rate limit" in text or "too many requests" in text:
        return "FC-3002", True
    return "FC-3003", True


class CircuitBreaker:
    """closed (normal) → open (skip the LLM for a while) → half-open (one test call) → closed."""

    def __init__(self, failures: int, open_seconds: int):
        self.max_failures, self.open_seconds = failures, open_seconds
        self.failures, self.opened_at = 0, None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if time.time() - self.opened_at >= self.open_seconds:
                log.info("circuit breaker half-open: letting one test call through")
                return True
            return False

    def success(self):
        with self._lock:
            if self.opened_at is not None:
                log.info("circuit breaker CLOSED ✓ (LLM healthy again)")
            self.failures, self.opened_at = 0, None

    def failure(self):
        with self._lock:
            self.failures += 1
            if self.failures >= self.max_failures:
                if self.opened_at is None:
                    log.warning(f"circuit breaker OPEN ✗ after {self.failures} failures in a row; "
                                f"using fallback for {self.open_seconds} s")
                self.opened_at = time.time()


class ResilientLLM(BaseLLM):
    """Wraps a generative provider with timeout + retry + circuit breaker + extractive fallback."""

    def __init__(self, primary: BaseLLM):
        s = get_settings()
        self.primary = primary
        self.fallback = ExtractiveLLM()
        self.breaker = CircuitBreaker(s.breaker_failures, s.breaker_open_seconds)
        self.name = primary.name

    def answer(self, question, vehicle, context, sources, feedback=""):
        s = get_settings()
        code = "FC-3004"
        if not self.breaker.allow():
            log.warning("FC-3004 circuit breaker open → skipping LLM call")
        else:
            for attempt in range(1, s.llm_max_retries + 2):
                try:
                    log.info(f"  ├─ LLM call attempt {attempt} → {self.primary.name}")
                    result = self.primary.answer(question, vehicle, context, sources, feedback)
                    self.breaker.success()
                    log.info(f"  └─ LLM call ✓ {result.latency_ms} ms | in_tok={result.input_tokens} "
                             f"out_tok={result.output_tokens}")
                    return result
                except Exception as e:
                    code, retry = classify_error(e)
                    self.breaker.failure()
                    log.warning(f"  ├─ {code} LLM call attempt {attempt} failed: {e.__class__.__name__}: {str(e)[:200]}")
                    if retry and attempt <= s.llm_max_retries:
                        wait = random.uniform(0.2, 0.8)          # jittered backoff avoids retry storms
                        log.info(f"  ├─ retrying in {wait:.2f} s")
                        time.sleep(wait)
                        continue
                    break
        if not s.fallback_to_extractive:
            from app.core.errors import AppError
            raise AppError(code, "LLM unavailable and fallback disabled")
        log.warning(f"  └─ {code} → answering in extractive fallback mode (quotes the sources, still cited)")
        result = self.fallback.answer(question, vehicle, context, sources)
        result.degraded, result.error_code = "extractive_fallback", code
        return result

    def rewrite_query(self, question):
        if not self.breaker.allow():
            return None
        try:
            return self.primary.rewrite_query(question)
        except Exception as e:
            log.warning(f"query rewrite skipped: {e.__class__.__name__}: {str(e)[:120]}")
            return None


# =============================================================== factory
_llm: BaseLLM | None = None


def get_llm() -> BaseLLM:
    """Create the configured LLM client once and reuse it."""
    global _llm
    if _llm is None:
        provider = get_settings().llm_provider.lower()
        providers = {"bedrock": BedrockLLM, "openai": OpenAILLM, "ollama": OllamaLLM, "extractive": ExtractiveLLM}
        if provider not in providers:
            raise ValueError(f"Unknown LLM_PROVIDER: {provider} (use bedrock | openai | ollama | extractive)")
        instance = providers[provider]()
        _llm = ResilientLLM(instance) if instance.is_generative else instance
        log.info(f"LLM ready ✓ | provider={_llm.name} resilient={isinstance(_llm, ResilientLLM)}")
    return _llm
