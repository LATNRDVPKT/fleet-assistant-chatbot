"""
Ingestion command line.

  python -m app.ingestion.cli validate                 # check manifest + files, no indexing
  python -m app.ingestion.cli ingest                   # incremental ingest (changed docs only)
  python -m app.ingestion.cli ingest --rebuild         # rebuild the whole index
  python -m app.ingestion.cli ingest --doc VM-EV60-OM  # one document
  python -m app.ingestion.cli inspect --doc VM-EV60-OM # print its chunks (debug chunking)
  python -m app.ingestion.cli stats                    # index summary
"""
import argparse
import json

from app.core.logging import get_logger
from app.core.registry import manifest
from app.ingestion.indexer import run_ingestion, validate_manifest
from app.retrieval.stores import load_chunks, load_meta

log = get_logger("ingestion.cli")


def main():
    parser = argparse.ArgumentParser(description="Fleet Copilot document ingestion")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--rebuild", action="store_true")
    p_ing.add_argument("--doc")
    p_ins = sub.add_parser("inspect")
    p_ins.add_argument("--doc", required=True)
    p_ins.add_argument("--limit", type=int, default=50)
    sub.add_parser("stats")
    args = parser.parse_args()

    if args.cmd == "validate":
        errors = validate_manifest(manifest()["documents"])
        for e in errors:
            log.error(e)
        log.info("manifest OK ✓" if not errors else f"{len(errors)} error(s)")
        raise SystemExit(1 if errors else 0)

    if args.cmd == "ingest":
        run_ingestion(rebuild=args.rebuild, only_doc=args.doc)

    if args.cmd == "inspect":
        for c in [c for c in load_chunks() if c["metadata"]["doc_id"] == args.doc][: args.limit]:
            m = c["metadata"]
            print(f"\n=== {c['chunk_id']} | {m['content_type']} | page {m['page_start']}-{m['page_end']} "
                  f"| {m['section_path']} | {m['word_count']} words")
            print(c["text"])

    if args.cmd == "stats":
        meta = load_meta()
        print(json.dumps({k: v for k, v in meta.items() if k != "documents"}, indent=2))
        for doc_id, d in meta.get("documents", {}).items():
            print(f"  {doc_id:<16} vehicle={d['vehicle_model']:<8} v{d['version']:<5} chunks={d['chunks']}")


if __name__ == "__main__":
    main()
