# ---------------------------------------------------------------------
# Fleet Copilot API image  (FastAPI + RAG pipeline + ingestion)
# Build:  docker build -t fleet-copilot-api .
# ---------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/models \
    INDEX_DIR=/app/data/index

WORKDIR /app

# 1) CPU-only PyTorch first (much smaller than the default GPU build)
RUN pip install --no-cache-dir torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu

# 2) Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Download the embedding model and the reranker model at BUILD time,
#    so containers never download anything when they start.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2'); print('models downloaded')"

# 4) Application code, configuration and the source documents
COPY app ./app
COPY config ./config
COPY scripts ./scripts
COPY data/documents ./data/documents
COPY evals ./evals

# 5) Run as a normal user, not root
RUN useradd --create-home appuser && mkdir -p /app/data/index && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else 1)"

CMD ["./scripts/start_api.sh"]
