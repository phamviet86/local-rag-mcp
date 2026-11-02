# Design: legal-structure chunking, citations and retrieval evaluation

Status: implementation notes for branch `feat/legal-structure-search` (no version change).

## Goal

An agent searching Vietnamese legal text should land on the right Điều/khoản, cite it with a
stable key (document number, article, clause, page, OCR label), and be able to measure that quality.

## Schema (migration 8, appended to `MIGRATIONS`)

- `chunks`: `cite_key TEXT`, `section_path TEXT` (JSON array), `page_start INTEGER`,
  `page_end INTEGER`, `ocr INTEGER NOT NULL DEFAULT 0`; index `(document_id, cite_key)`.
- `documents`: `doc_no TEXT`, `doc_no_key TEXT`; index on `doc_no_key`.
- `page_number` is unchanged (= `page_start` when a chunk sits on one page).
- Existing rows keep NULL until `reindex --reextract`. Chunk text changes (context line), so
  `chunk_hash` changes and embeddings are recomputed.

## `local_rag.legal` (stdlib only)

- `find_numbers`, `make_key` (folds Đ→D, lowercases, strips diacritics), `detect_headings`,
  `split_sections`, `make_cite_key`.
- Document number pattern: serial with optional thousands dot, optional year, hyphenated group code
  starting with letters (`66.13/2026/NQ-CP`, `24/2026/VBHN-TT-BTC`, `135/VBHN-TT-BCT`); dates such
  as `01/04/2025` do not match.
- Cross-reference guard: a heading `Điều N` only counts when N is the previous number + 1 (or the
  same number with a letter suffix), the title is short (<200 chars) and the previous line ends a
  sentence. Articles quoted inside amending text are not split.
- One Điều per chunk; split by khoản when longer than `structure_max_chars` (default 2000). Each
  chunk starts with a context line `số hiệu | tên | Chương | Điều`. Markdown splits on `#` headings.
  Fewer than two headings falls back to fixed windows.
- Cite keys: `{doc_no}#dieu-5-khoan-2-diem-a`; without a number `{stable_ref}#...`; windows
  `{stable_ref}#c{ordinal}`; duplicates get `~2`, `~3`.

## Config

`chunk_strategy` = `structure` (default, falls back to `window`) | `window`;
`structure_max_chars` = 2000. The legacy `_chunks` stays for the v0.9 fixture.

## Search / surfaces

- `search(match="auto"|"any"|"all"|"phrase", doc_no=None)`; default `auto` lists all-terms matches first, then partial matches. A query
  that is a document number, or `doc_no`, looks up `documents.doc_no_key` first and ranks that
  document's chunks first.
- Results add `cite_key`, `page_start`, `page_end`, `ocr`, `section_path`, `doc_no`.
- MCP reader tool `get_chunk` (read-only); CLI `chunk`, `search --match --doc-no`, `eval`.
- `eval <file.jsonl>`: `{id, question, gold:[cite_key], type, diacritics}` → recall@1/3/10, misses,
  suggested failure groups; exit 1 below `--target`.

## Findings from real PDFs (pdf-inspector 1.25.2)

- Headings arrive as Markdown (`## Điều 1. ...`, bold `**Điều 2.** ...` inline after a sentence,
  `**Chương II** **TITLE** **Điều 3...**` on one line). The splitter normalises this before
  detection and maps offsets back to the original text.
- Running page headers sit between articles; consolidated texts skip repealed article numbers; forms in
  appendices restart numbering. Detection therefore accepts a forward jump of up to 10 and ignores
  lower numbers.
- Preamble text becomes a `mo-dau` chunk; pieces over `structure_max_chars` split by khoản, điểm,
  then paragraphs (`-ptN`).
- OCR (PP-OCRv6 small): CER about 0.14 on a synthetic Vietnamese scan with dropped vowels.

## Unverified

Large-corpus performance and scanned real legal PDFs.
