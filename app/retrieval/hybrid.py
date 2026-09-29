"""
Hybrid retrieval engine (the "RETRIEVAL ENGINE" box in the architecture):

  metadata filters → 7a vector search ┐
                   → 7b BM25 search   ┴→ 7c RRF fusion → 7d reranking (+ own-manual boost) → ranked chunks

Fallbacks (the request keeps going, a WARN is logged):
  vector search fails  → FC-3006, keyword (BM25) results only
  reranker fails       → FC-3007, keep the fused RRF order
  both searches fail   → AppError FC-5003 (HTTP 500)
"""
import time

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.retrieval.embeddings import get_embedder
from app.retrieval.reranker import get_reranker
from app.retrieval.stores import KeywordIndex, VectorIndex, load_chunks, load_meta

log = get_logger("retrieval.hybrid")


class IndexNotReady(RuntimeError):
    pass


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


class HybridRetriever:
    def __init__(self):
        s = get_settings()
        log.info(f"loading index from {s.index_dir}")
        self.meta = load_meta()
        chunks = load_chunks()
        if not chunks:
            raise IndexNotReady(f"FC-5001 no index found in {s.index_dir}. Run: python -m app.ingestion.cli ingest")
        self.embedder = get_embedder()
        if self.meta.get("embedding") != self.embedder.name:
            raise IndexNotReady(f"FC-5002 index built with '{self.meta.get('embedding')}', but EMBEDDING_PROVIDER "
                                f"gives '{self.embedder.name}'. Set it to match, or re-run ingestion with --rebuild.")
        self.by_id = {c["chunk_id"]: c for c in chunks}
        self.keyword = KeywordIndex(chunks)
        self.vector = VectorIndex()
        self.reranker = get_reranker()
        log.info(f"retriever ready ✓ | chunks={len(chunks)} index_version={self.meta.get('index_version')} "
                 f"embedding={self.embedder.name} reranker={self.reranker.name}")

    def retrieve(self, query: str, rerank_query: str, filters: dict | None, top_n: int,
                 prefer_vehicle: str | None = None, candidate_k: int | None = None) -> tuple[list[dict], dict]:
        """Returns (ranked chunks, stats for the trace)."""
        s = get_settings()
        k = candidate_k or s.candidate_k
        stats: dict = {}

        # 7a — vector (semantic) search
        t = time.perf_counter()
        vector_error = None
        try:
            vec_hits = self.vector.search(self.embedder.embed_one(query), k, filters)
        except Exception as e:
            vector_error = e
            vec_hits = []
            stats["degraded"] = "bm25_only"
            log.warning(f"  ├─ FC-3006 vector search failed ({e.__class__.__name__}: {e}); using BM25 only")
        stats["vector_hits"], stats["vector_ms"] = len(vec_hits), _ms(t)
        log.info(f"  ├─ 7a vector search   {stats['vector_ms']:>7} ms | hits={len(vec_hits)} "
                 f"top={vec_hits[0][0] + ' ' + str(vec_hits[0][1]) if vec_hits else '-'}")

        # 7b — keyword (BM25) search
        t = time.perf_counter()
        try:
            bm25_hits = self.keyword.search(query, k, filters)
        except Exception as e:
            if vector_error is not None:
                raise AppError("FC-5003", f"both vector and keyword search failed: {e}") from e
            bm25_hits = []
            log.warning(f"  ├─ BM25 search failed ({e.__class__.__name__}: {e}); using vector results only")
        stats["bm25_hits"], stats["bm25_ms"] = len(bm25_hits), _ms(t)
        log.info(f"  ├─ 7b BM25 search     {stats['bm25_ms']:>7} ms | hits={len(bm25_hits)} "
                 f"top={bm25_hits[0][0] + ' ' + str(round(bm25_hits[0][1], 2)) if bm25_hits else '-'}")

        # 7c — Reciprocal Rank Fusion: score = Σ 1 / (rrf_k + rank)
        t = time.perf_counter()
        fused: dict[str, dict] = {}
        for source, hits in (("vector", vec_hits), ("bm25", bm25_hits)):
            for rank, (cid, score) in enumerate(hits, start=1):
                e = fused.setdefault(cid, {"chunk_id": cid, "rrf": 0.0, "vector_score": None, "bm25_score": None})
                e["rrf"] += 1.0 / (s.rrf_k + rank)
                e[f"{source}_score"] = round(score, 4)
        candidates = sorted(fused.values(), key=lambda e: e["rrf"], reverse=True)[: max(k, top_n)]
        in_both = sum(1 for c in candidates if c["vector_score"] is not None and c["bm25_score"] is not None)
        stats["fused"] = len(candidates)
        log.info(f"  ├─ 7c RRF fusion      {_ms(t):>7} ms | candidates={len(candidates)} found_by_both={in_both}")
        if not candidates:
            log.info("  └─ no candidates matched the filters")
            return [], stats

        # 7d — rerank + boost the asked vehicle's own manual
        t = time.perf_counter()
        docs = [self.by_id[c["chunk_id"]]["embed_text"] for c in candidates]
        try:
            rerank = self.reranker.score(rerank_query, docs)
        except Exception as e:
            rerank = [None] * len(docs)
            stats["degraded"] = "no_rerank"
            log.warning(f"  ├─ FC-3007 reranker failed ({e.__class__.__name__}: {e}); keeping RRF order")
        max_rrf = max(c["rrf"] for c in candidates)
        boosted = 0
        for c, r in zip(candidates, rerank):
            chunk = self.by_id[c["chunk_id"]]
            c.update(text=chunk["text"], metadata=chunk["metadata"])
            base = r if r is not None else c["rrf"] / max_rrf
            boost = s.vehicle_specific_boost if prefer_vehicle and chunk["metadata"]["vehicle_model"] == prefer_vehicle else 0.0
            boosted += boost > 0
            c["rerank_score"] = r
            c["score"] = round(min(1.0, base + boost), 4)
        ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)
        stats["rerank_ms"] = _ms(t)
        log.info(f"  └─ 7d rerank + boost  {stats['rerank_ms']:>7} ms | reranker={self.reranker.name} "
                 f"boosted={boosted} top={ranked[0]['chunk_id']} score={ranked[0]['score']}")
        return ranked[: top_n * 3], stats          # the validator trims to top_n after dropping bad chunks
