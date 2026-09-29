"""Request / response models — FastAPI rejects invalid requests with HTTP 422 automatically."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.core.registry import find_vehicle, vehicle_models


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=2, description="The user's question")
    vehicle_model: str | None = Field(None, description="Selected vehicle model, e.g. EV-60")
    variant: str | None = None
    region: str | None = "India"
    session_id: str | None = Field(None, max_length=64)
    debug: bool = False

    @field_validator("question")
    @classmethod
    def _question_length(cls, v: str) -> str:
        v = v.strip()
        if len(v) > get_settings().max_question_chars:
            raise ValueError(f"question longer than {get_settings().max_question_chars} characters")
        if len(v) < 2:
            raise ValueError("question is empty")
        return v

    @field_validator("vehicle_model")
    @classmethod
    def _known_vehicle(cls, v: str | None) -> str | None:
        if v in (None, "", "none"):
            return None
        match = find_vehicle(v)
        if not match:
            raise ValueError(f"unknown vehicle_model '{v}'. Known: {vehicle_models()}")
        return match["model"]


class Citation(BaseModel):
    id: str
    chunk_id: str
    doc_id: str
    doc_name: str
    doc_title: str
    doc_version: str
    vehicle_model: str
    section: str
    page: str
    content_type: str
    score: float
    snippet: str


class ChatResponse(BaseModel):
    request_id: str
    status: Literal["answered", "clarification_needed", "not_found", "blocked"]
    code: str | None = Field(None, description="FC code for non-answered outcomes, e.g. FC-2005")
    answer: str
    citations: list[Citation] = []
    vehicle_model: str | None = None
    intent: str | None = None
    confidence: str = "n/a"
    grounded: bool | None = None
    degraded: str | None = Field(None, description="Set when a fallback was used, e.g. extractive_fallback")
    latency_ms: float
    trace: list[dict] | None = None


class FeedbackRequest(BaseModel):
    request_id: str = Field(..., min_length=4, max_length=64)
    rating: Literal["up", "down"]
    comment: str | None = Field(None, max_length=1000)
