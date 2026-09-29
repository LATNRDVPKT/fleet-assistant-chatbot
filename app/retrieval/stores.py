"""
The two indexes that make up hybrid retrieval.

  VectorIndex  → Chroma (persistent, cosine). Semantic similarity.
  KeywordIndex → BM25 over chunks.jsonl. Exact terms: DTC codes, "0.30 g", "HX-400", "AdBlue".

Both live under INDEX_DIR:
  index/chroma/          Chroma files
  index/chunks.jsonl     one chunk per line (source of truth; also feeds BM25)
  index/index_meta.json  embedding provider, doc hashes, counts, build time
"""
import json
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.text import content_tokens

log = get_logger("retrieval.stores")
COLLECTION = "fleet_docs"


def _where(filters: dict | None) -> dict | None:
    """{"vehicle_model": ["EV-60", "ALL"], "region": "India"} → Chroma where clause."""
    if not filters:
        return None
    clauses = []
    for key, value in filters.items():
        clauses.append({key: {"$in": list(value)}} if isinstance(value, (list, tuple, set)) else {key: value})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def matches(meta: dict, filters: dict | None) -> bool:
    for key, value in (filters or {}).items():
        allowed = value if isinstance(value, (list, tuple, set)) else [value]
        if meta.get(key) not in allowed:
            return False
    return True


class VectorIndex:
    def __init__(self, index_dir: Path | None = None):
        import sys
        import chromadb
        from chromadb.config import Settings as ChromaSettings
        path = Path(index_dir or get_settings().index_dir) / "chroma"
        path.mkdir(parents=True, exist_ok=True)
        settings_kwargs = {"anonymized_telemetry": False}
        if sys.platform == "win32":
            # chromadb's default Rust bindings crash with an access violation on native
            # Windows (chroma-core/chroma#6052); fall back to the legacy hnswlib-backed API.
            settings_kwargs["chroma_api_impl"] = "chromadb.api.segment.SegmentAPI"
        self.client = chromadb.PersistentClient(path=str(path), settings=ChromaSettings(**settings_kwargs))
        self.col = self.client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"},
                                                        embedding_function=None)

    def count(self) -> int:
        return self.col.count()

    def upsert(self, ids, embeddings, documents, metadatas, batch: int = 200):
        for i in range(0, len(ids), batch):
            self.col.upsert(ids=ids[i:i + batch], embeddings=embeddings[i:i + batch],
                            documents=documents[i:i + batch], metadatas=metadatas[i:i + batch])

    def delete_doc(self, doc_id: str):
        self.col.delete(where={"doc_id": doc_id})

    def search(self, query_embedding: list[float], k: int, filters: dict | None) -> list[tuple[str, float]]:
        n = min(k, max(1, self.count()))
        res = self.col.query(query_embeddings=[query_embedding], n_results=n, where=_where(filters),
                             include=["distances"])
        return [(cid, round(1.0 - dist, 4)) for cid, dist in zip(res["ids"][0], res["distances"][0])]


class KeywordIndex:
    def __init__(self, chunks: list[dict]):
        from rank_bm25 import BM25Okapi
        self.chunks = chunks
        self.bm25 = BM25Okapi([content_tokens(c["embed_text"]) for c in chunks]) if chunks else None

    def search(self, query: str, k: int, filters: dict | None) -> list[tuple[str, float]]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(content_tokens(query))
        ranked = sorted(
            ((c["chunk_id"], float(s)) for c, s in zip(self.chunks, scores) if s > 0 and matches(c["metadata"], filters)),
            key=lambda x: x[1], reverse=True)
        return ranked[:k]


def load_chunks(index_dir: Path | None = None) -> list[dict]:
    path = Path(index_dir or get_settings().index_dir) / "chunks.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_meta(index_dir: Path | None = None) -> dict:
    path = Path(index_dir or get_settings().index_dir) / "index_meta.json"
    return json.loads(path.read_text()) if path.exists() else {}
