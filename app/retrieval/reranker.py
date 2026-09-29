"""
Rerankers score (query, chunk) pairs after fusion. Scores are mapped to 0..1.

  cross-encoder → sentence-transformers CrossEncoder (best quality; default)
  lexical       → share of the question's key words found in the chunk (offline/test)
  none          → keep the fused RRF order
"""
import math

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.text import content_tokens, numbers_in

log = get_logger("retrieval.reranker")


class LexicalReranker:
    name = "lexical"

    def score(self, question: str, docs: list[str]) -> list[float]:
        q = set(content_tokens(question))
        q_nums = numbers_in(question)
        out = []
        for d in docs:
            d_toks = set(content_tokens(d))
            overlap = len(q & d_toks) / len(q) if q else 0.0
            if q_nums:                                   # exact numbers / codes matter a lot
                overlap = 0.8 * overlap + 0.2 * (len(q_nums & numbers_in(d)) / len(q_nums))
            out.append(round(overlap, 4))
        return out


class CrossEncoderReranker:
    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder
        log.info(f"loading cross-encoder: {model_name}")
        self.model = CrossEncoder(model_name, max_length=512, device="cpu")
        self.name = f"cross-encoder:{model_name}"

    def score(self, question: str, docs: list[str]) -> list[float]:
        logits = self.model.predict([(question, d[:2000]) for d in docs])
        return [round(1 / (1 + math.exp(-float(x))), 4) for x in logits]


class NoReranker:
    name = "none"

    def score(self, question: str, docs: list[str]) -> list[float]:
        return [None] * len(docs)                        # hybrid.py falls back to normalised RRF


_reranker = None


def get_reranker():
    """Create the configured reranker once and reuse it."""
    global _reranker
    if _reranker is None:
        kind = get_settings().reranker.lower()
        if kind == "cross-encoder":
            try:
                _reranker = CrossEncoderReranker(get_settings().reranker_model)
            except Exception as e:                       # model not downloadable → degrade, don't crash
                log.warning(f"FC-3007 cross-encoder unavailable ({e.__class__.__name__}: {e}); using lexical reranker")
                _reranker = LexicalReranker()
        elif kind == "lexical":
            _reranker = LexicalReranker()
        else:
            _reranker = NoReranker()
        log.info(f"reranker ready ✓ | {_reranker.name}")
    return _reranker
