"""
Shared test setup.

Tests run fully offline: hash embeddings, lexical reranker, extractive answers.
A fresh index is built ONCE per test session from the real documents into a temporary folder.
"""
import os
import tempfile

import pytest

# Must be set before any app module reads the settings.
_INDEX_DIR = tempfile.mkdtemp(prefix="fleet-test-index-")
os.environ.update({
    "EMBEDDING_PROVIDER": "hash",
    "RERANKER": "lexical",
    "LLM_PROVIDER": "extractive",
    "INDEX_DIR": _INDEX_DIR,
    "API_KEYS": "dev-key-123:demo-tenant,ratelimit-key:rl-tenant",
    "LOG_LEVEL": "WARNING",
})


@pytest.fixture(scope="session")
def index_dir():
    from app.ingestion.indexer import run_ingestion
    run_ingestion(rebuild=True)
    return _INDEX_DIR


@pytest.fixture(scope="session")
def pipeline(index_dir):
    from app.pipeline.orchestrator import CopilotPipeline
    return CopilotPipeline()


@pytest.fixture(scope="session")
def chunks(index_dir):
    from app.retrieval.stores import load_chunks
    return load_chunks()


@pytest.fixture(scope="session")
def client(index_dir):
    from fastapi.testclient import TestClient
    from app.api.main import app
    with TestClient(app) as c:
        yield c
