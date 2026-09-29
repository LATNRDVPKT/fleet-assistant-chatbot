"""
Document loaders: turn a .docx or .pdf into an ordered list of Elements.

An Element is one of: heading | paragraph | list_item | table.
Order is preserved (a table stays next to the heading/paragraph it belongs to),
which is what makes the later chunking "structure-aware".
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

log = get_logger("ingestion.loaders")

_NUMBERED_HEADING = re.compile(r"^((\d+\.)+\d*|\d+\.|[A-Z]\.\d+(\.\d+)*|Appendix [A-Z])\s")
_LIST_ITEM = re.compile(r"^(\d+\.|•|-|–)\s+")
_CAPTION = re.compile(r"^Table\s+[\dA-Z]+(\.\d+)*\s*[–:-]")


@dataclass
class Element:
    kind: str                      # heading | paragraph | list_item | table
    text: str = ""
    level: int = 0                 # heading level (1 = top)
    page: int | None = None        # 1-based page (PDF only)
    page_end: int | None = None
    header: list[str] = field(default_factory=list)       # table header row
    rows: list[list[str]] = field(default_factory=list)   # table body rows
    caption: str = ""


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


# ============================================================== DOCX
def load_docx(path: Path) -> list[Element]:
    from docx import Document
    from docx.table import Table

    doc = Document(str(path))
    elements: list[Element] = []
    skipping_toc = False
    pending_caption = ""
    n_par = n_tab = 0

    for block in doc.iter_inner_content():           # paragraphs AND tables in reading order
        if isinstance(block, Table):
            rows = []
            for row in block.rows:
                cells, seen = [], set()
                for c in row.cells:                    # merged cells repeat → keep each once
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc))
                    cells.append(_clean(c.text))
                rows.append(cells)
            rows = [r for r in rows if any(r)]
            if not rows or skipping_toc:
                continue
            elements.append(Element("table", header=rows[0], rows=rows[1:], caption=pending_caption))
            pending_caption = ""
            n_tab += 1
            continue

        text = _clean(block.text)
        if not text:
            continue
        style = (block.style.name or "").lower()
        if style.startswith("heading") or style == "title":
            level = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
            skipping_toc = "table of contents" in text.lower()
            if not skipping_toc:
                elements.append(Element("heading", text=text, level=level))
            continue
        if skipping_toc:
            continue
        if _CAPTION.match(text):
            pending_caption = text
            continue
        kind = "list_item" if "list" in style else "paragraph"
        elements.append(Element(kind, text=text))
        n_par += 1

    log.info(f"docx parsed: {path.name} | paragraphs={n_par} tables={n_tab} elements={len(elements)}")
    return elements


# ============================================================== PDF
def load_pdf(path: Path) -> list[Element]:
    import pdfplumber

    elements: list[Element] = []
    with pdfplumber.open(str(path)) as pdf:
        body_size = _body_font_size(pdf)
        log.info(f"pdf opened: {path.name} | pages={len(pdf.pages)} body_font={body_size}pt")

        for page_no, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            bboxes = [t.bbox for t in tables]

            def outside_tables(obj):
                x0, top, x1, bottom = obj.get("x0", 0), obj.get("top", 0), obj.get("x1", 0), obj.get("bottom", 0)
                return not any(b[0] - 1 <= x0 and x1 <= b[2] + 1 and b[1] - 1 <= top and bottom <= b[3] + 1 for b in bboxes)

            # 1) text lines outside tables, with font size → heading / caption / body
            items = []
            for line in page.filter(outside_tables).extract_text_lines(return_chars=True):
                size = Counter(round(c["size"], 1) for c in line["chars"]).most_common(1)[0][0]
                if size <= body_size - 2:              # running header / footer / page number
                    continue
                items.append({"top": line["top"], "kind": "line", "text": _clean(line["text"]), "size": size})
            # 2) tables
            for t in tables:
                rows = [[_clean(c) for c in r] for r in t.extract()]
                rows = [r for r in rows if any(r)]
                if rows:
                    items.append({"top": t.bbox[1], "kind": "table", "rows": rows})
            items.sort(key=lambda i: i["top"])

            prev_top, para = None, None
            pending_caption = ""
            for it in items:
                if it["kind"] == "table":
                    para = _flush(para, elements)
                    _add_pdf_table(elements, it["rows"], pending_caption, page_no, it["top"])
                    pending_caption = ""
                    prev_top = None
                    continue
                text, size = it["text"], it["size"]
                if size >= body_size + 2:                      # heading
                    para = _flush(para, elements)
                    m = _NUMBERED_HEADING.match(text)
                    level = m.group(1).rstrip(".").count(".") + 1 if m and m.group(1)[0].isdigit() else 1
                    if m and m.group(1)[0].isalpha() and "." in m.group(1):
                        level = 2
                    elements.append(Element("heading", text=text, level=level, page=page_no))
                elif _CAPTION.match(text):
                    para = _flush(para, elements)
                    pending_caption = text
                else:                                           # body line → paragraph building
                    new_para = (para is None or prev_top is None or it["top"] - prev_top > size * 1.6
                                or _LIST_ITEM.match(text))
                    if new_para:
                        para = _flush(para, elements)
                        kind = "list_item" if _LIST_ITEM.match(text) else "paragraph"
                        para = Element(kind, text=text, page=page_no)
                    else:
                        para.text += " " + text
                prev_top = it["top"]
            _flush(para, elements)

    log.info(f"pdf parsed: {path.name} | elements={len(elements)} "
             f"tables={sum(e.kind == 'table' for e in elements)}")
    return elements


def _flush(para, elements):
    if para is not None and para.text.strip():
        elements.append(para)
    return None


def _add_pdf_table(elements, rows, caption, page_no, top):
    """Append a table; stitch it onto the previous table if it continues from the previous page."""
    header, body = rows[0], rows[1:]
    prev = elements[-1] if elements else None
    continues = (prev is not None and prev.kind == "table" and not caption and top < 120
                 and prev.page_end == page_no - 1 and [h.lower() for h in header] == [h.lower() for h in prev.header])
    if continues:
        if body and body[0] and not body[0][0] and prev.rows:           # a row split across the page break
            prev.rows[-1] = [(a + " " + b).strip() for a, b in zip(prev.rows[-1], body[0])]
            body = body[1:]
        prev.rows.extend(body)
        prev.page_end = page_no
        log.debug(f"table continued onto page {page_no}: {prev.caption or prev.header[0]}")
    else:
        elements.append(Element("table", header=header, rows=body, caption=caption, page=page_no, page_end=page_no))


def _body_font_size(pdf) -> float:
    sizes = Counter()
    for page in pdf.pages[: min(5, len(pdf.pages))]:
        for c in page.chars:
            sizes[round(c["size"], 1)] += 1
    # body text = the most common size among sizes used for "paragraph" text (ignore tiny footers)
    common = [s for s, _ in sizes.most_common(4)]
    return max(common[:2]) if len(common) > 1 else common[0]


def load_document(path: Path) -> list[Element]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return load_docx(path)
    if suffix == ".pdf":
        return load_pdf(path)
    raise ValueError(f"Unsupported file type: {suffix} (use .pdf or .docx)")
