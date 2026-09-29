"""API contract: status codes and the standard error envelope."""
from app.core.config import get_settings

H = {"X-API-Key": "dev-key-123"}


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["chunks"] >= 150


def test_missing_key_is_401_with_envelope(client):
    r = client.post("/v1/chat", json={"question": "hello there"})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "FC-1001" and err["retryable"] is False and err["request_id"]


def test_invalid_key_is_401(client):
    r = client.post("/v1/chat", json={"question": "hello there"}, headers={"X-API-Key": "nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "FC-1002"


def test_unknown_vehicle_is_422(client):
    r = client.post("/v1/chat", json={"question": "oil interval", "vehicle_model": "ZX-9"}, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "FC-1005"
    assert "ZX-9" in r.json()["error"]["detail"]


def test_too_long_question_is_422(client):
    r = client.post("/v1/chat", json={"question": "x" * 1001}, headers=H)
    assert r.status_code == 422


def test_answer_with_citations(client):
    r = client.post("/v1/chat", json={"question": "What is the cold tyre pressure for the steer axle?",
                                      "vehicle_model": "HX-400"}, headers=H)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "answered"
    assert "8.3 bar" in body["answer"] and body["citations"][0]["doc_id"] == "VM-HX400-OM"
    assert r.headers["X-Request-ID"] == body["request_id"]


def test_rate_limit_is_429_with_retry_after(client):
    s = get_settings()
    old = s.rate_limit_per_minute
    s.rate_limit_per_minute = 2
    try:
        codes = [client.post("/v1/chat", json={"question": "What is AIS-140?"},
                             headers={"X-API-Key": "ratelimit-key"}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
    finally:
        s.rate_limit_per_minute = old


def test_overload_is_503(client):
    from app.api.main import state
    state["inflight"] = get_settings().max_inflight_requests
    try:
        r = client.post("/v1/chat", json={"question": "What is AIS-140?"}, headers=H)
        assert r.status_code == 503 and r.json()["error"]["code"] == "FC-5004"
        assert r.headers["Retry-After"] == "2"
    finally:
        state["inflight"] = 0


def test_feedback(client):
    r = client.post("/v1/feedback", json={"request_id": "abc12345", "rating": "up"}, headers=H)
    assert r.json() == {"status": "recorded"}
