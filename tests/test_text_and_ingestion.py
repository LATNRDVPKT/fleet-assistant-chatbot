"""Text helpers, loaders, chunking and manifest validation."""
from app.core.text import content_tokens, numbers_in
from app.ingestion.indexer import validate_manifest


def test_tokenizer_keeps_compounds_and_parts():
    toks = content_tokens("HX-400 battery-temperature at 0.30 g")
    assert "hx-400" in toks and "hx" in toks and "400" in toks
    assert "battery" in toks and "temperature" in toks
    assert "0.30" in toks


def test_numbers_are_normalised():
    assert numbers_in("40,000 km") == {"40000"}
    assert numbers_in("0.30 g") == numbers_in("0.3 g")


def test_all_documents_indexed(chunks):
    docs = {c["metadata"]["doc_id"] for c in chunks}
    assert docs == {"FLEET-HANDBOOK", "VM-HX400-OM", "VM-EV60-OM", "VM-CX32-OM"}


def test_every_table_chunk_has_caption_and_header(chunks):
    tables = [c for c in chunks if c["metadata"]["content_type"] == "table"]
    assert len(tables) >= 40
    for c in tables:
        assert c["text"].startswith("**") or "**" in c["text"]          # caption line
        assert "|---" in c["text"]                                     # markdown header separator
        assert "Columns:" in c["embed_text"]                           # row-wise search text


def test_pdf_table_split_across_pages_is_stitched(chunks):
    table = next(c for c in chunks if "Table 8.1 – HX-400 telematics thresholds" in c["text"])
    assert (table["metadata"]["page_start"], table["metadata"]["page_end"]) == (3, 4)
    rows = [l for l in table["text"].splitlines() if l.startswith("| ") and "---" not in l]
    assert len(rows) == 10                                             # header + all 9 rows, none lost
    assert "Coolant temperature" in table["text"] and "AdBlue level" in table["text"]


def test_row_split_by_page_break_is_merged():
    from app.ingestion.loaders import Element, _add_pdf_table
    elements = [Element("table", header=["Temp", "Status", "Effect"],
                        rows=[["≥ 50 °C", "Warning", "Amber lamp;"]], caption="Table 5.2", page=2, page_end=2)]
    # next page: repeated header, then the rest of the split row (first cell empty), then a new row
    _add_pdf_table(elements, [["Temp", "Status", "Effect"], ["", "", "power derate begins."],
                              ["≥ 55 °C", "Critical", "Stop"]], caption="", page_no=3, top=60)
    assert len(elements) == 1
    t = elements[0]
    assert t.rows[0] == ["≥ 50 °C", "Warning", "Amber lamp; power derate begins."]
    assert t.rows[1][0] == "≥ 55 °C" and t.page_end == 3


def test_docx_table_of_contents_is_skipped(chunks):
    assert not any("Table of Contents" in c["metadata"]["section_path"] for c in chunks)


def test_no_double_bullets(chunks):
    assert not any("- •" in c["text"] for c in chunks)


def test_manifest_validation_reports_bad_entries():
    bad = [{"doc_id": "X", "path": "data/documents/missing.pdf", "title": "t", "doc_type": "manual",
            "vehicle_model": "ZZ-1", "region": "India", "version": "1"}]
    errors = " ".join(validate_manifest(bad))
    assert "not in config/vehicles.yaml" in errors
    assert "doc_type" in errors
    assert "file not found" in errors
