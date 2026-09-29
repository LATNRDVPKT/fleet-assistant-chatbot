"""
Error catalogue.  Every failure the API can return has a stable code (FC-xxxx).

Clients should branch on `code`, never on the message text.

  FC-1xxx  client problems (auth, quota, validation)       → HTTP 4xx
  FC-2xxx  business outcomes (blocked, not found, ...)      → HTTP 200 with a `status`
  FC-3xxx  a dependency failed but we recovered             → logged, response still 200
  FC-5xxx  the service could not answer                     → HTTP 5xx
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorInfo:
    name: str
    http_status: int
    retryable: bool
    message: str


CATALOG: dict[str, ErrorInfo] = {
    "FC-1001": ErrorInfo("AUTH_MISSING", 401, False, "Missing X-API-Key header."),
    "FC-1002": ErrorInfo("AUTH_INVALID", 401, False, "Your access key is invalid. Contact your fleet admin."),
    "FC-1004": ErrorInfo("RATE_LIMITED", 429, True, "Too many requests. Please retry after a few seconds."),
    "FC-1005": ErrorInfo("VALIDATION_FAILED", 422, False, "The request is not valid."),
    "FC-2001": ErrorInfo("BLOCKED_UNSAFE", 200, False, "Unsafe request blocked."),
    "FC-2002": ErrorInfo("BLOCKED_INJECTION", 200, False, "Instruction-override attempt blocked."),
    "FC-2003": ErrorInfo("UNSUPPORTED_LANGUAGE", 200, False, "Only English is supported right now."),
    "FC-2004": ErrorInfo("CLARIFICATION_NEEDED", 200, False, "Please choose a vehicle."),
    "FC-2005": ErrorInfo("NOT_FOUND", 200, False, "Not found in the approved documents."),
    "FC-2006": ErrorInfo("UNGROUNDED_REFUSED", 200, False, "Could not produce a verified answer."),
    "FC-3001": ErrorInfo("LLM_TIMEOUT", 503, True, "The language model timed out."),
    "FC-3002": ErrorInfo("LLM_THROTTLED", 503, True, "The language model is busy."),
    "FC-3003": ErrorInfo("LLM_ERROR", 503, True, "The language model returned an error."),
    "FC-3004": ErrorInfo("CIRCUIT_OPEN", 503, True, "The language model is temporarily disabled."),
    "FC-3006": ErrorInfo("EMBEDDING_FAILED", 503, True, "Semantic search failed; keyword search used."),
    "FC-3007": ErrorInfo("RERANKER_FAILED", 503, True, "Reranker failed; fused order used."),
    "FC-5001": ErrorInfo("INDEX_NOT_READY", 503, True, "The service is starting or the index is missing."),
    "FC-5002": ErrorInfo("INDEX_EMBEDDING_MISMATCH", 503, False, "Index was built with a different embedding model."),
    "FC-5003": ErrorInfo("INTERNAL_ERROR", 500, True, "Something went wrong. Please try again."),
    "FC-5004": ErrorInfo("OVERLOADED", 503, True, "The assistant is busy. Please retry in a few seconds."),
}


class AppError(Exception):
    """Raise this anywhere; the API turns it into the standard error envelope."""

    def __init__(self, code: str, detail: str | None = None, retry_after: int | None = None):
        self.code = code
        self.info = CATALOG[code]
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(f"{code} {self.info.name}: {detail or self.info.message}")

    def envelope(self, request_id: str) -> dict:
        body = {"code": self.code, "name": self.info.name, "message": self.info.message,
                "retryable": self.info.retryable, "request_id": request_id}
        if self.detail:
            body["detail"] = self.detail
        return {"error": body}
