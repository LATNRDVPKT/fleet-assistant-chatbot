"""
Structure-aware chunking.

TEXT
  * A chunk never crosses a section boundary (heading), so every chunk has one clear section.
  * Paragraphs are split into sentences; sentences are packed up to CHUNK_TARGET_WORDS.
  * The last N sentences are repeated at the start of the next chunk (overlap) for continuity.

TABLES
  * A table is never cut mid-row. Small tables = 1 chunk; big tables = row groups of
    TABLE_ROWS_PER_CHUNK, and the header row is repeated in every group.
  * Two representations are stored:
      - `text`        → Markdown table (what the LLM reads and what the UI shows)
      - `embed_text`  → one line per row, "Header: value; Header: value" (what gets embedded
                        and BM25-indexed; row-wise text retrieves far better than raw grids)
  * A short intro paragraph right before a table (< 40 words) is folded into the table chunk.

Every chunk carries a context header (document, vehicle, section) in `embed_text`, so a
chunk like "| Every 10,000 km | Chassis greasing |" is still findable by "HX-400 service".
"""
import hashlib
import re
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.text import split_sentences
from app.ingestion.loaders import Element

log = get_logger("ingestion.chunker")
_NUMBERED = re.compile(r"^\d+\.\s")


@dataclass
class Chunk:
    chunk_id: str
    text: str                     # shown to the LLM / user
    embed_text: str               # used for embeddings + BM25
    metadata: dict = field(default_factory=dict)


class StructureAwareChunker:
    def __init__(self, doc_meta: dict):
        s = get_settings()
        self.meta = doc_meta
        self.target_words = s.chunk_target_words
        self.overlap = s.chunk_overlap_sentences
        self.rows_per_chunk = s.table_rows_per_chunk
        self.chunks: list[Chunk] = []
        self.section_path: list[str] = []
        self._buf: list[tuple[str, int | None]] = []   # (sentence, page) waiting to be chunked
        self._seed = 0                                 # how many sentences in _buf are overlap
        self._emitted_in_section = False

    # ------------------------------------------------------------ public
    def chunk(self, elements: list[Element]) -> list[Chunk]:
        for el in elements:
            if el.kind == "heading":
                self._flush_text(final=True)
                self._enter_heading(el)
            elif el.kind in ("paragraph", "list_item"):
                sentences = [el.text] if el.kind == "list_item" else split_sentences(el.text)
                for sent in sentences:
                    if el.kind == "list_item" and not _NUMBERED.match(sent):
                        sent = "- " + re.sub(r"^[•\-–]\s*", "", sent)      # one clean "- " bullet
                    self._buf.append((sent, el.page))
                    if self._words() >= self.target_words:
                        self._flush_text(final=False)
            elif el.kind == "table":
                self._add_table(el)
        self._flush_text(final=True)
        n_text = sum(c.metadata["content_type"] == "text" for c in self.chunks)
        log.info(f"chunked {self.meta['doc_id']}: {len(self.chunks)} chunks "
                 f"(text={n_text}, table={len(self.chunks) - n_text})")
        return self.chunks

    # ------------------------------------------------------------ sections
    def _enter_heading(self, el: Element):
        level = max(1, el.level)
        self.section_path = self.section_path[: level - 1] + [el.text]
        self._emitted_in_section = False

    @property
    def section(self) -> str:
        return self.section_path[-1] if self.section_path else "Front matter"

    # ------------------------------------------------------------ text
    def _words(self) -> int:
        return sum(len(s.split()) for s, _ in self._buf)

    def _flush_text(self, final: bool):
        """final=True at a section end: emit what is left (unless it is only overlap).
        final=False when the buffer is full: emit it and keep the last N sentences as overlap."""
        if final:
            if len(self._buf) > self._seed:
                self._emit_text(self._buf)
            self._buf, self._seed = [], 0
        else:
            self._emit_text(self._buf)
            tail = self._buf[-self.overlap:] if self.overlap else []
            self._buf, self._seed = list(tail), len(tail)

    def _emit_text(self, batch):
        body = ""
        for sent, _ in batch:
            sep = "\n" if (sent.startswith("- ") or _NUMBERED.match(sent)) else " "
            body = (body + sep + sent) if body else sent
        pages = [p for _, p in batch if p]
        self._add_chunk(body.strip(), body.strip(), "text", pages)
        self._emitted_in_section = True

    # ------------------------------------------------------------ tables
    def _add_table(self, el: Element):
        intro = ""
        if self._buf and not self._emitted_in_section and self._words() < 40:   # fold short intro into the table
            intro = " ".join(s for s, _ in self._buf)
            self._buf, self._seed = [], 0
        else:
            self._flush_text(final=True)

        header = el.header
        caption = el.caption or f"Table in section: {self.section}"
        groups = [el.rows[i:i + self.rows_per_chunk] for i in range(0, len(el.rows), self.rows_per_chunk)] or [[]]
        for gi, rows in enumerate(groups, start=1):
            part = f" (part {gi}/{len(groups)})" if len(groups) > 1 else ""
            md = [f"**{caption}{part}**", "", "| " + " | ".join(header) + " |",
                  "|" + "---|" * len(header)]
            md += ["| " + " | ".join(r + [""] * (len(header) - len(r))) + " |" for r in rows]
            linear = [f"{caption}{part}. Columns: {', '.join(header)}."]
            for r in rows:
                pairs = [f"{h}: {v.rstrip('.')}" for h, v in zip(header, r) if v]
                linear.append("; ".join(pairs) + ".")
            text = ((intro + "\n\n") if intro else "") + "\n".join(md)
            embed = ((intro + "\n") if intro else "") + "\n".join(linear)
            pages = [p for p in (el.page, el.page_end) if p]
            self._add_chunk(text, embed, "table", pages, table_caption=caption, table_rows=len(rows))
        self._emitted_in_section = True

    # ------------------------------------------------------------ common
    def _add_chunk(self, text: str, embed_body: str, content_type: str, pages: list[int], **extra):
        m = self.meta
        seq = len(self.chunks)
        vehicle = m["vehicle_model"] if m["vehicle_model"] != "ALL" else "All vehicles"
        header = f"Document: {m['title']} | Vehicle: {vehicle} | Section: {' > '.join(self.section_path) or 'Front matter'}"
        metadata = {
            "doc_id": m["doc_id"],
            "doc_name": m["file_name"],
            "doc_title": m["title"],
            "doc_type": m["doc_type"],
            "doc_version": str(m["version"]),
            "vehicle_model": m["vehicle_model"],
            "variants": ",".join(m.get("variants", [])),
            "region": m.get("region", ""),
            "section": self.section,
            "section_path": " > ".join(self.section_path),
            "page_start": min(pages) if pages else -1,       # -1 = not paginated (docx)
            "page_end": max(pages) if pages else -1,
            "content_type": content_type,
            "chunk_index": seq,
            "word_count": len(text.split()),
            "content_hash": hashlib.sha1(text.encode()).hexdigest()[:12],
            **extra,
        }
        self.chunks.append(Chunk(f"{m['doc_id']}::{seq:04d}", text, header + "\n" + embed_body, metadata))
