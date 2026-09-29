"""
Step 10 — Context builder: order the validated chunks, give each a source id [S1]..[Sn],
and render the SOURCES block the LLM will read (within a character budget).
"""
from app.core.config import get_settings


def _page_label(m: dict) -> str:
    if m.get("page_start", -1) in (-1, None):
        return "n/a (docx)"
    return str(m["page_start"]) if m["page_start"] == m["page_end"] else f"{m['page_start']}-{m['page_end']}"


def build_context(chunks: list[dict]) -> tuple[list[dict], str]:
    budget = get_settings().max_context_chars
    sources, blocks, used = [], [], 0
    for c in chunks:
        m = c["metadata"]
        sid = f"S{len(sources) + 1}"
        vehicle = "All vehicles" if m["vehicle_model"] == "ALL" else m["vehicle_model"]
        header = (f"[{sid}] {m['doc_title']} (v{m['doc_version']}) | Vehicle: {vehicle} | "
                  f"Section: {m['section_path']} | Page: {_page_label(m)}")
        block = f"{header}\n{c['text']}"
        if used + len(block) > budget and sources:
            break
        used += len(block)
        blocks.append(block)
        sources.append({
            "id": sid,
            "chunk_id": c["chunk_id"],
            "doc_id": m["doc_id"],
            "doc_name": m["doc_name"],
            "doc_title": m["doc_title"],
            "doc_version": m["doc_version"],
            "vehicle_model": m["vehicle_model"],
            "section": m["section_path"],
            "page": _page_label(m),
            "content_type": m["content_type"],
            "score": c["score"],
            "text": c["text"],
        })
    return sources, "\n\n".join(blocks)
