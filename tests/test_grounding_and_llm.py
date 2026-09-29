"""Grounding validator and LLM resilience (retry, circuit breaker, fallback)."""
from app.pipeline import grounding
from app.pipeline.llm import CircuitBreaker, ExtractiveLLM, LLMResult, ResilientLLM
from app.pipeline.prompts import NOT_FOUND

SOURCES = [{"id": "S1", "text": "**Table 4.1 – HX-400 tyre pressures**\n\n| Position | Cold pressure | Min tread |\n|---|---|---|\n"
            "| Steer axle | 8.3 bar (120 psi) | 3.0 mm |", "section": "4. Tyre Specifications",
            "doc_title": "HX-400 manual", "content_type": "table"}]


def test_supported_claim_passes():
    g = grounding.check("- Steer axle cold pressure is 8.3 bar (120 psi) [S1]", SOURCES)
    assert g["grounded"] and g["ratio"] == 1.0


def test_number_not_in_source_is_flagged():
    g = grounding.check("- Steer axle cold pressure is 9.1 bar (120 psi) [S1]", SOURCES)
    assert not g["grounded"]
    assert "9.1" in g["unsupported"][0]["reason"]


def test_invalid_citation_is_flagged():
    g = grounding.check("- Steer axle cold pressure is 8.3 bar [S7]", SOURCES)
    assert not g["grounded"] and g["invalid_citations"] == ["S7"]


def test_refusal_counts_as_grounded():
    assert grounding.check(NOT_FOUND, SOURCES)["grounded"]


class FlakyLLM:
    """Fake provider that fails the first `fail_times` calls."""
    name = "fake:flaky"
    is_generative = True

    def __init__(self, fail_times, error="ThrottlingException: rate exceeded"):
        self.calls, self.fail_times, self.error = 0, fail_times, error

    def answer(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(self.error)
        return LLMResult("Steer axle pressure is 8.3 bar [S1]", self.name)


def _resilient(fake, index_dir):
    llm = ResilientLLM.__new__(ResilientLLM)          # build without loading config-driven providers
    llm.primary, llm.fallback, llm.name = fake, ExtractiveLLM(), fake.name
    llm.breaker = CircuitBreaker(failures=3, open_seconds=60)
    return llm


def test_throttling_is_retried_once_then_succeeds(index_dir):
    fake = FlakyLLM(fail_times=1)
    r = _resilient(fake, index_dir).answer("steer pressure", "HX-400", "", SOURCES)
    assert fake.calls == 2 and r.degraded is None


def test_persistent_failure_falls_back_to_extractive(index_dir):
    fake = FlakyLLM(fail_times=99)
    r = _resilient(fake, index_dir).answer("steer axle pressure", "HX-400", "", SOURCES)
    assert r.degraded == "extractive_fallback" and r.error_code == "FC-3002"
    assert "8.3 bar" in r.text and "[S1]" in r.text


def test_timeout_is_not_retried(index_dir):
    fake = FlakyLLM(fail_times=99, error="ReadTimeoutError: read timed out")
    r = _resilient(fake, index_dir).answer("steer axle pressure", "HX-400", "", SOURCES)
    assert fake.calls == 1 and r.error_code == "FC-3001"


def test_circuit_breaker_opens_and_skips_calls(index_dir):
    fake = FlakyLLM(fail_times=99)
    llm = _resilient(fake, index_dir)
    for _ in range(3):
        llm.answer("steer axle pressure", "HX-400", "", SOURCES)
    calls_before = fake.calls
    r = llm.answer("steer axle pressure", "HX-400", "", SOURCES)
    assert fake.calls == calls_before                   # breaker open → provider not called
    assert r.error_code == "FC-3004"
