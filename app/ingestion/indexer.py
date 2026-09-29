"""
Ingestion pipeline:  manifest → validate → detect changes → parse → chunk → embed → index.

Incremental by default: a document is re-processed only when its file hash (or manifest
entry) changes. Use --rebuild to start from an empty index.
"""
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import PROJECT_ROOT, get_settings
from app.core.logging import get_logger
from app.core.registry import manifest, vehicle_models
from app.ingestion.chunker import StructureAwareChunker
from app.ingestion.loaders import load_document
from app.retrieval.embeddings import get_embedder
from app.retrieval.stores import VectorIndex, load_chunks, load_meta

log = get_logger("ingestion.indexer")

REQUIRED_FIELDS = ["doc_id", "path", "title", "doc_type", "vehicle_model", "region", "version"]
DOC_TYPES = {"vehicle_manual", "fleet_handbook", "policy", "sop"}


def _sha256(path: Path, entry: dict) -> str:
    h = hashlib.sha256(path.read_bytes())
    h.update(json.dumps(entry, sort_keys=True).encode())        # metadata edits also trigger re-ingest
    return h.hexdigest()


def validate_manifest(docs: list[dict]) -> list[str]:
    errors, seen = [], set()
    known = set(vehicle_models()) | {"ALL"}
    for i, d in enumerate(docs):
        where = d.get("doc_id", f"entry #{i}")
        for f in REQUIRED_FIELDS:
            if not d.get(f):
                errors.append(f"{where}: missing field '{f}'")
        if d.get("doc_id") in seen:
            errors.append(f"{where}: duplicate doc_id")
        seen.add(d.get("doc_id"))
        if d.get("vehicle_model") and d["vehicle_model"] not in known:
            errors.append(f"{where}: vehicle_model '{d['vehicle_model']}' not in config/vehicles.yaml")
        if d.get("doc_type") and d["doc_type"] not in DOC_TYPES:
            errors.append(f"{where}: doc_type must be one of {sorted(DOC_TYPES)}")
        p = PROJECT_ROOT / d.get("path", "")
        if not p.is_file():
            errors.append(f"{where}: file not found: {d.get('path')}")
        elif p.suffix.lower() not in (".pdf", ".docx"):
            errors.append(f"{where}: unsupported file type {p.suffix}")
    return errors


def run_ingestion(rebuild: bool = False, only_doc: str | None = None) -> dict:
    s = get_settings()
    t0 = time.perf_counter()
    index_dir = Path(s.index_dir)
    log.info("=" * 78)
    log.info(f"INGESTION START | index_dir={index_dir} | embedding={s.embedding_provider} | rebuild={rebuild}")
    log.info("=" * 78)

    # ---- [1/6] validate manifest
    docs = manifest()["documents"]
    if only_doc:
        docs = [d for d in docs if d["doc_id"] == only_doc]
    errors = validate_manifest(docs)
    if errors:
        for e in errors:
            log.error(f"[1/6] manifest error: {e}")
        raise SystemExit(f"Manifest has {len(errors)} error(s); fix config/manifest.yaml")
    log.info(f"[1/6] manifest valid ✓ | documents={len(docs)}")

    # ---- [2/6] detect changes
    if rebuild and index_dir.exists():
        shutil.rmtree(index_dir)
        log.info("[2/6] --rebuild: old index deleted")
    index_dir.mkdir(parents=True, exist_ok=True)
    meta = load_meta(index_dir)
    embedder = get_embedder()
    if meta and meta.get("embedding") != embedder.name:
        raise SystemExit(f"Index was built with '{meta.get('embedding')}' but EMBEDDING_PROVIDER gives "
                         f"'{embedder.name}'. Re-run with --rebuild.")
    old_hashes = meta.get("documents", {})
    todo = []
    for d in docs:
        digest = _sha256(PROJECT_ROOT / d["path"], d)
        if old_hashes.get(d["doc_id"], {}).get("sha256") == digest:
            log.info(f"[2/6] unchanged, skip: {d['doc_id']}")
        else:
            todo.append((d, digest))
            log.info(f"[2/6] {'new' if d['doc_id'] not in old_hashes else 'changed'}: {d['doc_id']}")

    vectors = VectorIndex(index_dir)
    all_chunks = [c for c in load_chunks(index_dir) if c["metadata"]["doc_id"] not in {d["doc_id"] for d, _ in todo}]
    summary = []

    for d, digest in todo:
        path = PROJECT_ROOT / d["path"]
        # ---- [3/6] parse
        tp = time.perf_counter()
        elements = load_document(path)
        log.info(f"[3/6] parsed {d['doc_id']} ✓ | elements={len(elements)} "
                 f"tables={sum(e.kind == 'table' for e in elements)} | {(time.perf_counter() - tp) * 1000:.0f} ms")
        # ---- [4/6] chunk
        chunks = StructureAwareChunker({**d, "file_name": path.name}).chunk(elements)
        log.info(f"[4/6] chunked {d['doc_id']} ✓ | chunks={len(chunks)}")
        # ---- [5/6] embed
        te = time.perf_counter()
        embeddings = embedder.embed([c.embed_text for c in chunks])
        log.info(f"[5/6] embedded {d['doc_id']} ✓ | vectors={len(embeddings)} dim={len(embeddings[0])} "
                 f"| {(time.perf_counter() - te) * 1000:.0f} ms")
        # ---- [6/6] write to vector index (replace this doc's old chunks)
        vectors.delete_doc(d["doc_id"])
        ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for c in chunks:
            c.metadata["ingested_at"] = ingested_at
        vectors.upsert([c.chunk_id for c in chunks], embeddings, [c.text for c in chunks], [c.metadata for c in chunks])
        all_chunks += [{"chunk_id": c.chunk_id, "text": c.text, "embed_text": c.embed_text, "metadata": c.metadata}
                       for c in chunks]
        old_hashes[d["doc_id"]] = {"sha256": digest, "chunks": len(chunks), "path": d["path"],
                                   "vehicle_model": d["vehicle_model"], "version": str(d["version"]),
                                   "ingested_at": ingested_at}
        summary.append((d["doc_id"], d["vehicle_model"], len(elements), len(chunks),
                        sum(c.metadata["content_type"] == "table" for c in chunks)))
        log.info(f"[6/6] indexed {d['doc_id']} ✓")

    # remove documents that were deleted from the manifest
    if not only_doc:
        for gone in set(old_hashes) - {d["doc_id"] for d in docs}:
            vectors.delete_doc(gone)
            all_chunks = [c for c in all_chunks if c["metadata"]["doc_id"] != gone]
            old_hashes.pop(gone)
            log.info(f"[6/6] removed document no longer in manifest: {gone}")

    with open(index_dir / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in sorted(all_chunks, key=lambda c: c["chunk_id"]):
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    new_meta = {
        "index_version": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "embedding": embedder.name,
        "chunk_count": len(all_chunks),
        "vector_count": vectors.count(),
        "documents": old_hashes,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (index_dir / "index_meta.json").write_text(json.dumps(new_meta, indent=2))

    # ---- verification: BM25 corpus and vector index must agree
    if new_meta["chunk_count"] != new_meta["vector_count"]:
        log.error(f"MISMATCH: chunks.jsonl={new_meta['chunk_count']} vs chroma={new_meta['vector_count']}")
        raise SystemExit("Index verification failed")

    log.info("-" * 78)
    log.info(f"{'doc_id':<16}{'vehicle':<10}{'elements':>9}{'chunks':>8}{'tables':>8}")
    for row in summary:
        log.info(f"{row[0]:<16}{row[1]:<10}{row[2]:>9}{row[3]:>8}{row[4]:>8}")
    log.info("-" * 78)
    log.info(f"INGESTION DONE ✓ | processed={len(todo)} total_chunks={new_meta['chunk_count']} "
             f"| {(time.perf_counter() - t0):.1f} s")
    return new_meta
