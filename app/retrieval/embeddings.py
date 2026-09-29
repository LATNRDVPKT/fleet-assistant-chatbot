"""
Embedding providers (all return L2-normalised float vectors).

  local   → sentence-transformers model on CPU (default; baked into the Docker image)
  bedrock → Amazon Titan Text Embeddings v2 via Bedrock
  hash    → deterministic hashing vectoriser; no downloads, no network. Used by tests/CI.

IMPORTANT: the index must be queried with the same provider it was built with.
The ingestion CLI writes the provider into index_meta.json and the API checks it at startup.
"""
import numpy as np

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("retrieval.embeddings")


class Embedder:
    name = "base"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class LocalEmbedder(Embedder):
    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        log.info(f"loading sentence-transformers model: {model_name}")
        self.model = SentenceTransformer(model_name, device="cpu")
        self.name = f"local:{model_name}"

    def embed(self, texts):
        return self.model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False).tolist()


class BedrockEmbedder(Embedder):
    def __init__(self, model_id: str, region: str):
        import boto3
        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id
        self.name = f"bedrock:{model_id}"

    def embed(self, texts):
        import json
        out = []
        for t in texts:
            resp = self.client.invoke_model(modelId=self.model_id,
                                            body=json.dumps({"inputText": t[:8000], "normalize": True}))
            out.append(json.loads(resp["body"].read())["embedding"])
        return out


class HashEmbedder(Embedder):
    """Word + character n-gram hashing. Weak semantics, but fully offline and deterministic."""

    def __init__(self, dim: int = 1024):
        from sklearn.feature_extraction.text import HashingVectorizer
        self.word = HashingVectorizer(n_features=dim, ngram_range=(1, 2), alternate_sign=False, norm=None)
        self.char = HashingVectorizer(n_features=dim, analyzer="char_wb", ngram_range=(3, 5), alternate_sign=False, norm=None)
        self.name = f"hash:{dim}"

    def embed(self, texts):
        m = self.word.transform(texts).toarray() * 2.0 + self.char.transform(texts).toarray() * 0.5
        m = np.log1p(m)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (m / norms).tolist()


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """Create the configured embedder once (loading a model takes seconds) and reuse it."""
    global _embedder
    if _embedder is None:
        s = get_settings()
        provider = s.embedding_provider.lower()
        log.info(f"creating embedder | provider={provider}")
        if provider == "local":
            _embedder = LocalEmbedder(s.embedding_model)
        elif provider == "bedrock":
            _embedder = BedrockEmbedder(s.bedrock_embedding_model, s.aws_region)
        elif provider == "hash":
            _embedder = HashEmbedder()
        else:
            raise ValueError(f"Unknown EMBEDDING_PROVIDER: {provider} (use local | bedrock | hash)")
        log.info(f"embedder ready ✓ | {_embedder.name}")
    return _embedder
