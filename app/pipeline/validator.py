"""
Step 8 — Context validation: relevance, metadata, vehicle compatibility, confidence, duplicates.
Decides "sufficient" (→ context builder) or "insufficient" (→ retrieval recovery).
"""
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.text import jaccard

log = get_logger("pipeline.validator")

REQUIRED_META = ("doc_id", "doc_name", "section", "vehicle_model", "doc_version")


def validate(candidates: list[dict], plan: dict) -> tuple[list[dict], dict]:
    s = get_settings()
    allowed_vehicles = set(plan["filters"].get("vehicle_model", [])) or None
    kept: list[dict] = []
    dropped = {"low_relevance": 0, "incompatible_vehicle": 0, "missing_metadata": 0, "duplicate": 0}

    for c in candidates:
        m = c["metadata"]
        reason = None
        if allowed_vehicles and m["vehicle_model"] not in allowed_vehicles:     # defence in depth
            reason = "incompatible_vehicle"
        elif any(not m.get(f) for f in REQUIRED_META):
            reason = "missing_metadata"
        elif c["score"] < s.min_relevance:
            reason = "low_relevance"
        elif any(m["content_hash"] == k["metadata"]["content_hash"] or jaccard(c["text"], k["text"]) > 0.85 for k in kept):
            reason = "duplicate"
        if reason:
            dropped[reason] += 1
            log.debug(f"  ├─ drop {c['chunk_id']} score={c['score']} reason={reason}")
            continue
        kept.append(c)
        log.info(f"  ├─ keep {c['chunk_id']:<22} score={c['score']:<6} {m['content_type']:<5} "
                 f"page={m['page_start']} section=\"{m['section'][:50]}\"")
        if len(kept) == plan["top_k"]:
            break

    top = candidates[0]["score"] if candidates else 0.0
    report = {"kept": len(kept), "top_score": round(top, 3), **{f"dropped_{k}": v for k, v in dropped.items() if v},
              "sufficient": bool(kept) and kept[0]["score"] >= s.min_relevance}
    return kept, report
