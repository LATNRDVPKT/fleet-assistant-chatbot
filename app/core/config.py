"""
Central configuration.

Every setting below can be overridden with an environment variable of the same name
(upper-case), or in a `.env` file.  Example:  LLM_PROVIDER=bedrock  TOP_K=6

Nothing in this project caches answers: every question runs the full pipeline.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---------------- app ----------------
    app_name: str = "Fleet Assistance Copilot"
    app_version: str = "3.0.0"
    environment: str = "local"                 # local | dev | prod
    log_level: str = "INFO"                    # DEBUG shows even more detail
    log_format: str = "text"                   # text (easy to read) | json (for CloudWatch)

    # ---------------- paths ----------------
    manifest_path: Path = PROJECT_ROOT / "config" / "manifest.yaml"
    vehicles_path: Path = PROJECT_ROOT / "config" / "vehicles.yaml"
    glossary_path: Path = PROJECT_ROOT / "config" / "glossary.yaml"
    index_dir: Path = PROJECT_ROOT / "data" / "index"

    # ---------------- API gateway ----------------
    api_keys: str = "dev-key-123:demo-tenant"  # "key:tenant,key2:tenant2"
    rate_limit_per_minute: int = 60            # per API key
    max_question_chars: int = 1000
    max_inflight_requests: int = 32            # per worker process; above this → 503 FC-5004

    # ---------------- ingestion / chunking ----------------
    chunk_target_words: int = 180              # text chunk size
    chunk_overlap_sentences: int = 1           # sentences repeated between text chunks
    table_rows_per_chunk: int = 12             # big tables are split into row groups

    # ---------------- embeddings ----------------
    embedding_provider: str = "local"          # local (sentence-transformers) | bedrock | hash (offline/tests)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    bedrock_embedding_model: str = "amazon.titan-embed-text-v2:0"

    # ---------------- retrieval ----------------
    top_k: int = 5                             # chunks sent to the LLM
    candidate_k: int = 20                      # candidates from each of vector + BM25
    rrf_k: int = 60                            # reciprocal rank fusion constant
    reranker: str = "cross-encoder"            # cross-encoder | lexical | none
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    vehicle_specific_boost: float = 0.10       # prefer the vehicle's own manual over the handbook
    min_relevance: float = 0.20                # context validation threshold (0..1)
    max_retrieval_retries: int = 2

    # ---------------- LLM ----------------
    llm_provider: str = "extractive"           # bedrock | openai | ollama | extractive (offline, no LLM)
    bedrock_model_id: str = "anthropic.claude-haiku-4-5-20251001-v1:0"
    aws_region: str = "ap-south-1"
    openai_model: str = "gpt-4o-mini"
    openai_api_key: str = ""
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 700
    max_context_chars: int = 9000

    # ---------------- LLM resilience ----------------
    llm_timeout_seconds: int = 20              # one LLM call may take at most this long
    llm_max_retries: int = 1                   # retries on throttling / server errors (not on timeouts)
    breaker_failures: int = 5                  # consecutive failures that open the circuit breaker
    breaker_open_seconds: int = 30             # how long the breaker stays open before a test call
    fallback_to_extractive: bool = True        # if the LLM fails, answer by quoting the sources

    # ---------------- grounding ----------------
    grounding_min_ratio: float = 0.8           # share of claims that must be supported
    claim_support_threshold: float = 0.6       # word overlap needed for one claim
    max_regenerations: int = 1

    # ---------------- monitoring ----------------
    metrics_namespace: str = "FleetCopilot"
    emit_emf_metrics: bool = False             # True on AWS → CloudWatch metrics from log lines


_settings: Settings | None = None


def get_settings() -> Settings:
    """Load settings once (from env / .env) and reuse them."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Used by tests after changing environment variables."""
    global _settings
    _settings = Settings()
    return _settings
