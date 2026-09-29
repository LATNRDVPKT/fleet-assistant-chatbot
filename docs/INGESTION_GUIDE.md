# Ingestion guide: how documents become searchable

```
config/manifest.yaml ─► [1/6] validate ─► [2/6] detect changes (SHA-256) ─► [3/6] parse (PDF/DOCX)
                     ─► [4/6] chunk (text + tables) ─► [5/6] embed ─► [6/6] write Chroma + chunks.jsonl ─► verify
```

## Commands

```bash
python -m app.ingestion.cli validate                  # check manifest + files only
python -m app.ingestion.cli ingest                    # only new / changed documents
python -m app.ingestion.cli ingest --rebuild          # everything from scratch (needed after changing EMBEDDING_PROVIDER)
python -m app.ingestion.cli ingest --doc VM-EV60-OM   # one document
python -m app.ingestion.cli inspect --doc VM-EV60-OM  # print its chunks: check tables and sections look right
python -m app.ingestion.cli stats                     # index summary
```

In Docker, ingestion runs automatically when the API container starts (`scripts/start_api.sh`).

## Add a new vehicle model (no code change)

1. **Register the model** in `config/vehicles.yaml`:
   ```yaml
   - model: TX-12
     name: Trucko TX-12 Tipper
     category: Tipper (diesel)
     powertrain: diesel
     aliases: ["tx-12", "tx12", "trucko"]
     variants: ["6x4"]
   ```
2. **Add the file** to `data/documents/`. It must be a text-based PDF (not a scan) or a .docx.
   For best results: numbered headings (`5.`, `5.1`), tables with ruled borders and a header row, and a
   caption line `Table 5.1 – ...` above each table.
3. **Register the document** in `config/manifest.yaml`:
   ```yaml
   - doc_id: VM-TX12-OM
     path: data/documents/VM-TX12_Operator_Manual.pdf
     title: Trucko TX-12 Tipper – Operator Manual
     doc_type: vehicle_manual          # vehicle_manual | fleet_handbook | policy | sop
     vehicle_model: TX-12              # must exist in vehicles.yaml, or ALL for fleet-wide documents
     variants: ["6x4"]
     region: India
     version: "1.0"
     effective_date: "2026-10-01"
   ```
4. Run `validate`, then `ingest`, then `inspect --doc VM-TX12-OM`.
5. Add about 10 questions for the model to `evals/golden_set.jsonl` and run `python -m evals.run_evals`.

## What the loaders do

| Format | Text | Headings | Tables | Page numbers |
| --- | --- | --- | --- | --- |
| DOCX | paragraphs in reading order (tables stay next to their heading) | Word heading styles; Table of Contents skipped | cell text; merged cells de-duplicated; "Table x –" caption attached | not available → citations show the section |
| PDF | text lines outside tables, joined into paragraphs | font ≥ body + 2 pt; level from numbering (5.1 → level 2) | pdfplumber ruled tables; a table continuing on the next page is **stitched**, and a row split by the page break is **merged** | yes |

Running headers/footers (font ≤ body − 2 pt) are dropped.

## How chunks are made

| | Text | Tables |
| --- | --- | --- |
| Boundary | never crosses a heading | one table, or 12-row groups of a big table |
| Size | ~180 words, split on sentences | ≤ 12 rows, header repeated |
| Overlap | 1 sentence | none (rows are self-contained) |
| Stored for the LLM | the sentences | caption + Markdown table |
| Stored for search | `Document | Vehicle | Section` header + text | same header + caption + one line per row: `Header: value; Header: value.` |
| Extra | — | a short intro paragraph (< 40 words) right before the table is folded into it |

Every chunk carries metadata: `doc_id, doc_title, doc_version, vehicle_model, variants, region, section,
section_path, page_start, page_end, content_type, table_caption, content_hash, ingested_at`.

Current index: 4 documents → 160 chunks (113 text, 47 table).

## Example ingestion log (local embedding model; timings vary by machine)

```text
[1/6] manifest valid ✓ | documents=4
[2/6] changed: VM-EV60-OM
pdf opened: VM-EV60_Electric_Delivery_Van_Operator_Manual.pdf | pages=6 body_font=10.5pt
[3/6] parsed VM-EV60-OM ✓ | elements=62 tables=12 | 874 ms
chunked VM-EV60-OM: 23 chunks (text=11, table=12)
[5/6] embedded VM-EV60-OM ✓ | vectors=23 dim=384 | 310 ms
[6/6] indexed VM-EV60-OM ✓
INGESTION DONE ✓ | processed=1 total_chunks=160 | 2.1 s
```
