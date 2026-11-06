# Feature and command reference

This document describes the stable operator and agent-facing behavior of `local-rag-mcp`. Use the
CLI or MCP server as the integration boundary; Python modules under `local_rag` are implementation
details. Start with [deployment.md](deployment.md) for installation and [setup.md](setup.md) for
first-time configuration.

## Compatibility and data layout

The product, primary CLI, and MCP server remain `local-rag-mcp`. The Python distribution is
`phamviet-local-rag-mcp`, and `local-rag` remains an installed legacy CLI alias. Existing
`~/.local-rag`, `LOCAL_RAG_HOME`, and `LOCAL_RAG_*` embedding variables remain supported; new
deployments should use `LOCAL_RAG_MCP_HOME` and `LOCAL_RAG_MCP_*`.

The default data layout is:

```text
~/.local-rag/
  config.json
  index.sqlite3
  artifacts/extracted/       content-hash-addressed base extraction
  artifacts/revisions/       additive corrected PDF revisions
  cache/sources/<source-id>/  source-specific downloaded Drive bytes
  models/
  runtime/
```

Directories are restricted to owner access and SQLite/token files to owner read/write on POSIX.
Schema migration registers a legacy one-root index as source `default` in place; it does not move
source files, artifacts, reviews, revisions, metadata, or vectors.

## Source administration

```bash
local-rag-mcp source list
local-rag-mcp source add-local engineering /srv/engineering --exclude archive
local-rag-mcp source enable engineering
local-rag-mcp source disable engineering
local-rag-mcp source set engineering --poll-seconds 300 --full-reconcile-seconds 43200
local-rag-mcp source remove engineering --yes
```

Local roots are resolved and containment checked. Directory symlinks are not traversed; file
symlinks must resolve inside their registered root. The same local root cannot be registered twice.

Removing a source deletes only its database rows, unreferenced extracted/revision artifacts, orphan
vectors, and source-specific downloaded cache. It never deletes or modifies local files or Drive
items. Shared content-hash artifacts and vectors remain while another source references them.

### Google Drive roots and accounts

Each Drive source has its own root folder ID, account label, OAuth token path, change cursor, and
optional shared-drive ID. Install the `google-drive` extra from the same trusted release artifact,
then create a token with the read-only Drive scope:

```bash
local-rag-mcp auth-google \
  --client-secret /secure/google-desktop-client.json \
  --token-file "$HOME/.local-rag/credentials/work-account.json"

local-rag-mcp source add-drive work-drive DRIVE_ROOT_FOLDER_ID \
  --account work@example.com \
  --token-file "$HOME/.local-rag/credentials/work-account.json"
```

Client-secret and token contents are never stored in SQLite; only the token path is registered, and
that path is omitted from reader/status output. Token files must have mode `0600` on POSIX.
A service account is the alternative: `source add-drive ... --service-account-key PATH` (mutually
exclusive with `--token-file`, key mode `0600`); see [setup.md](setup.md).

Full sync walks the configured root, rejects incomplete listings and unsafe path segments, and
supports text/Markdown, PDF, DOCX, XLSX, PPTX, HTML, CSV, JSON, EPUB, ODT/ODS/ODP, Google Docs,
Sheets, and Slides. Incremental sync
consumes the Drive changes cursor and downloads/extracts only changed content fingerprints. If any
changed item fails download, extraction, or indexing, the durable cursor is not advanced, so the
change page can be retried safely.

## Reconcile, watch, reindex, and recovery

```bash
local-rag-mcp reconcile
local-rag-mcp reconcile --source engineering --background
local-rag-mcp sync --source work-drive
local-rag-mcp sync --source work-drive --full
local-rag-mcp reconcile reports --source engineering
local-rag-mcp serve

local-rag-mcp reindex --source engineering
local-rag-mcp reindex --source engineering --target reports/2026
local-rag-mcp reindex --source work-drive --target team/handbook
local-rag-mcp reindex --all
local-rag-mcp reindex --source work-drive --reextract
local-rag-mcp jobs list
local-rag-mcp jobs status JOB_ID
```

Indexing uses a durable single-writer queue. Job JSON includes an ID, phase, heartbeat,
discovered/processed/searchable/remaining counts, and embedding-pending count. A second identical
job coalesces; a conflicting job is rejected. SQLite WAL readers and MCP search/read continue from
the last committed index while a job runs.

Normal local reconciliation uses size and modification time as its unchanged fast path, then hashes
candidates. Targets are relative file or folder paths within a source. Partial Drive rebuilds do not
advance the source-wide changes cursor. A matching hash rebuilds chunks/FTS from the cached effective
artifact without rerunning extraction or OCR; `--reextract` explicitly opts into that work.

The `serve` daemon schedules each enabled source on its own timer, stored in the source config:
`poll_seconds` (default 300 for Drive, `reconcile_seconds` = 600 for local; minimum 60) and
`full_reconcile_seconds` (default 43200; Drive only, local reconciles always walk the tree). When a
source is due and no job is active, the daemon enqueues `reconcile(source)` (or `full` when the full
interval has elapsed since `last_full_reconcile_at`) and runs it itself. Sources added while the
daemon runs are scheduled on the next check; file watchers for new local sources need a daemon
restart. `local-rag-mcp refresh [--source NAME]` queues one rate-limited incremental reconcile
(60 seconds per source).

Native Watchdog observers coalesce writes for enabled local roots. Optional periodic reconciliation
also syncs Drive and recovers missed filesystem/change events. Manual CLI and MCP indexing do not
depend on the background service.

## Search, citations, and embeddings

Search is global unless a source and/or relative folder is supplied:

```bash
local-rag-mcp search "renewal clause"
local-rag-mcp search "renewal clause" --source legal-drive
local-rag-mcp search "renewal clause" --folder contracts/2026
local-rag-mcp search "renewal clause" --mode full_text
local-rag-mcp search "renewal clause" --mode semantic
local-rag-mcp read contract.pdf --source legal-drive
local-rag-mcp read legal-drive:contract.pdf
local-rag-mcp read legal-drive:old.pdf --include-trashed   # a Drive file in the trash
```

Legal-document options (see [design-legal-structure.md](design-legal-structure.md)):

```bash
local-rag-mcp search "đối tượng áp dụng" --match phrase      # auto (default) | any | all | phrase
local-rag-mcp search "66.13/2026/NQ-CP"                       # a document number is looked up first
local-rag-mcp search "tài sản công" --doc-no 99/2026/NĐ-CP   # rank that document's chunks first
local-rag-mcp chunk "99/2026/NĐ-CP#dieu-2" --neighbors 1      # read one chunk and its neighbours
local-rag-mcp eval questions.jsonl --k 3 --target 0.8         # exit 1 when recall@k is below target
```

Each hit carries `cite_key` (`<số hiệu>#dieu-5-khoan-2`, or `<stable_ref>#c<ordinal>` for windows),
`page_start`/`page_end`, `section_path`, `doc_no`, and `ocr`. OCR hits add `ocr_notice`: open the
original page before quoting. Rows indexed before this change have no `cite_key` until
`reindex --reextract`, which also changes chunk hashes so embeddings are recomputed. `chunk_strategy`
(`structure` or `window`), `structure_max_chars` and `legal_chunking` (default `true`) are settings in
`config.json`. With `legal_chunking: false`, the Vietnamese legal splitter (Điều/khoản) is skipped:
Markdown still splits by heading and every other file uses window chunks. Chunks are rebuilt from
the cached extraction, so run `reindex` (add `--reextract` to also refresh the extraction) after
changing it.
`eval` reads JSONL lines `{id, question, gold:[cite_key], type, diacritics}`; a gold Điều also accepts
its khoản chunks.

Validity and filters: `search --doc-type NĐ-CP`, `search --valid-on 2026-06-01` (only documents
with recorded validity covering the date), `search --match auto` (AND, then OR when empty).
Record validity with `validity set REF '{"effective_from":"2026-01-01","effective_to":"2027-01-01"}'
EVIDENCE_JSON --actor NAME` (reviewer capability; `effective_to` is exclusive) and check it with
`check-status REF --on 2026-06-01` (reader). Verify a quote against the original with
`page-image REF --page N --out page.png` (local PDFs; needs the PDFium runtime from `setup --full`). Missing dates mean `unknown`, never valid.

Modes are `full_text`, `semantic`, and `hybrid` (default). FTS5/BM25 remains local. Hybrid uses
reciprocal-rank fusion of the ordered BM25 and semantic result lists. If query embeddings fail,
hybrid returns FTS results with a warning; semantic-only fails clearly.

Folder scopes are literal safe relative paths. Supplying both source and folder applies both
filters strictly; omitting them searches every enabled source.

Each result includes `source`, `source_kind`, an unambiguous `document_ref`, match provenance, and a
citation containing source, external ID, authoritative URL where available, relative path, content
hash, PDF page, and source locators. The returned `document_ref` (`source:relative/path`) can be
passed directly to `read` or `metadata`.

Vectors remain in SQLite and are cached by chunk hash plus a fingerprint of provider, model,
endpoint, and declared dimensions. Missing unique embeddings are batched; local models load lazily;
query embeddings use a bounded in-process cache.

For a development checkout only:

```bash
python -m pip install -e '.[local-embeddings]'
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=local
export LOCAL_RAG_MCP_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

An OpenAI-compatible endpoint is also opt-in:

```bash
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=openai
export LOCAL_RAG_MCP_EMBEDDING_MODEL=provider/model
export LOCAL_RAG_MCP_OPENAI_BASE_URL=https://example.invalid/v1
export LOCAL_RAG_MCP_OPENAI_API_KEY='set-outside-the-repository'
```

Remote inference is never enabled implicitly. Enabling it sends indexed chunks and search queries
to the configured endpoint; vectors remain local.

## Extraction, OCR review, and evidence

Local and downloaded Drive files share the same content-hash extraction cache. TXT/Markdown, DOCX,
XLSX, and PPTX provenance records line, paragraph/table cell, sheet/cell, and slide/shape positions.
PDF inspection/native extraction uses `firecrawl/pdf-inspector==1.25.2`; only routed pages use the
pinned, checksum-verified local PDFium/ONNX/PP-OCRv6 runtime.

Blank, failed, complex, and low-quality pages enter a durable review queue:

```bash
local-rag-mcp ocr install
local-rag-mcp review list
local-rag-mcp review correct 12 "Corrected page text" \
  '[{"path":"legal-drive:scan.pdf","locator":"page:4","quote":"checked scan"}]' \
  --actor reviewer
```

Corrections create additive evidence/actor revisions, keep the base OCR artifact, and rebuild only
the affected searchable document. Default reindex preserves the effective correction, review
status, and revision history.

Metadata and relationships require non-empty evidence:

```bash
local-rag-mcp metadata add report.md status '"approved"' \
  '[{"path":"engineering:report.md","locator":"line:8","quote":"Approved"}]' \
  --source engineering

local-rag-mcp relationship report.md appendix.xlsx supports \
  '[{"path":"engineering:report.md","locator":"line:12"}]' \
  --source-source engineering --target-source finance
```

## MCP profiles and tools

The server uses the official Python MCP SDK over stdio. Select the narrowest profile. Profiles scope
the tools a process exposes; they are not authentication. For a single user driving the server over
SSH (see [deployment-vps.md](deployment-vps.md)), `LOCAL_RAG_MCP_PROFILE=admin` is the recommended
setting; use `reader` for ordinary retrieval clients.

```bash
LOCAL_RAG_MCP_PROFILE=reader local-rag-mcp-server
LOCAL_RAG_MCP_PROFILE=reviewer local-rag-mcp-server
LOCAL_RAG_MCP_PROFILE=admin local-rag-mcp-server
```

- `reader`: `search`, `list_documents`, `read`, `get_chunk`, `get_page_image`, `check_status`, `status`, `doctor`, `index_status`, `job_status`, `refresh`, `sources`,
  `metadata`, and `reviews`.
- `reviewer`: reader tools plus page correction/review resolution and evidence-backed metadata and
  relationships.
- `admin`: reviewer tools plus queued and synchronous reconcile/reindex, enable/disable, and
  confirmed source removal.

Tool annotations mark read-only, mutating, and destructive operations. Profiles are capability
exposure boundaries, not authentication. Source removal requires explicit confirmation and still
cannot delete authoritative source files.

## Current operational boundaries

Vectors are JSON arrays scored in process, suitable for a personal or team corpus rather than
millions of chunks. SQLite remains the intentional shared local engine; no external vector database
is required. Live Google OAuth/Drive behavior requires operator credentials and network access.
Windows is not a supported deployment target yet.

## v0.10.0 source safety and retrieval additions

Local reconciliation aborts without deleting indexed data when discovery cannot read the root
or a subtree. Source health records a sanitized error; restore access and retry reconciliation.

Drive sync now reports `metadata_updated` for location-only changes. Content keys use checksum/size
(or modified time and MIME for native files), never Drive version. v2 fingerprints upgrade in place.
Matching content updates paths and identity without extraction or downloads, retaining derived state.
Missing content evidence or changed MIME falls back to download; changed native modified time also
falls back safely. No live Drive rename assumption has been verified by the offline test suite.

Local reconcile reports `moved` for unique size/hash matches within a source. Matching runs before
extraction and retains document IDs, corrections, metadata and relationships. Duplicate content
or a move with an edit uses normal indexing/deletion; identity cannot be inferred safely.

Use `source relocate NAME NEW_PATH` (CLI) or admin MCP `relocate_source(source,new_path)`
to point an existing local source at a folder already moved by its operator. The durable job
validates discovery, rewrites document/review paths transactionally, then reconciles. It never
moves source files. Restart an existing watcher after relocation so its watched roots refresh.

Search, read and metadata expose `stable_ref`: `drive:<fileId>` or
`local:<immutable-source-id>:<document-id>`. Pass it as `path` to read/metadata after a move.
`document_ref` retains its existing source-name/path meaning. Coverage issues include Drive
stable refs. If one Drive ID is indexed in several sources, pass the source explicitly.

Vietnamese FTS accepts accented or unaccented queries, including `đ`/`d`. The migration rebuilds
FTS from stored text/metadata using unicode61 remove_diacritics 2; chunk text and vectors remain
unchanged. Original text is returned, with exact accent matches preferred within lexical candidates.

Content search hits now include `chunk_ordinal`, `start_char`, `end_char`, `heading_path` when
known, `indexed_at`, and `source_modified_at`. Metadata-only hits have null positions. Offsets
are Unicode character offsets into the effective indexed artifact, including OCR corrections.
Use `read(path=stable_ref, around=start_char, window=2000)` or CLI `read REF --around N --window N`
for context. Window is total characters centered on the hit, clamped at text boundaries.

Search accepts `per_document` (CLI `--per-document`, default 3, range 1..100). It caps primary
hits per document and collapses identical chunk hashes. Each content hit includes `locations`
with all matching enabled, in-scope document positions and citations, including copied files.

### File formats

Default extensions: `.txt .md .markdown .pdf .docx .xlsx .pptx` plus `.html .htm .csv .json .epub
.odt .ods .odp` (the new ones use the Python standard library only). Drive indexes the same formats
by MIME type (`text/html`, `text/csv`, `application/json`, `application/epub+zip` and the ODF types).
A home whose saved `extensions` equal the previous default list adopts the new defaults on load;
a customized list is left as saved.

| Format | Text | Locator |
| --- | --- | --- |
| HTML | visible text; `script`/`style` dropped, headings become Markdown headings | `block:N` |
| CSV | one `header: value; ...` line per data row | `row:N` |
| JSON | flattened `path: value` leaves (50 MiB / 200,000 value limit) | `$.a.b[0]` |
| EPUB | chapters in spine order, each through the HTML reader | `chapter:N` |
| ODT / ODS / ODP | paragraphs and headings / rows / slides from `content.xml` | `paragraph:N`, `sheet:S/row:N`, `slide:N` |

Legacy formats such as `.doc`, `.rtf` or `.xls` can be converted by the optional `markitdown` extra
(`pip install 'phamviet-local-rag-mcp[markitdown]'`). List the extensions in `config.json`, for
example `"markitdown_extensions": [".doc", ".rtf", ".xls"]` (default `[]`); each listed file becomes
one Markdown span (locator `document`). Without the library the affected files fail individually with
`code=markitdown_missing` in the report and coverage while the rest of the source indexes normally.
New extraction codes: `format_parse_failed`, `format_too_large`, `markitdown_missing`,
`markitdown_failed`.

### Drive page images and integrity

`get_page_image` also works for Drive PDFs when the raw download is still cached
(`cache/sources/<id>/raw/`); the result adds `cache_matches_index` (the cached file's sha256 equals
the indexed `content_hash`). Without the cache it fails with `page_image_unavailable`; run `refresh`
for the source and retry. `check_status` re-hashes the same cached file for Drive documents
(`integrity.checked: true`, `basis: drive_raw_cache`) and reports `checked: false` when it is absent.

### Layout flags

PDF pages with tables or columns no longer enter the review queue. Documents carry
`layout_flags: ["complex_layout"]` and `layout_pages` in their metadata, returned by `search`
(`layout_flags`) and `read` (`layout_flags`, `layout_pages`); `doctor` does not count them. OCR
reasons (`ocr_runtime_missing`, `low_quality_ocr`, ...) still create reviews. Migration 13 resolves
open `complex_layout` reviews with the note `migrated_to_layout_flag`.

DOCX extraction records heading paths without changing default paragraph-then-table text.
For existing files, explicit `reindex --source NAME --reextract` refreshes provenance. To emit
tables in document order, set `docx_document_order: true` in the operator-managed config.json,
restart the process and explicitly reextract affected DOCX files. This opt-in changes text and
may embed changed chunks. Its extraction cache is separate from the default representation.

Semantic search retains vector_json and adds a float32 BLOB copy through migration. The optional
`fast-search` extra enables NumPy matrix scoring; without it the original JSON scorer remains
available. A durable revision invalidates the per-provider/model matrix after index writes,
including writes by other processes. Float32 scores can differ at rounding precision.

Search adds `media_type`, `modified_after` and `modified_before` (CLI uses hyphenated flags).
MIME matches extracted media type exactly. Dates accept ISO 8601; dates or naive timestamps
use UTC, bounds are exclusive. Drive uses its source modified time; unknown times do not match
a date filter. All filters apply to lexical, semantic and duplicate-location results.

Coverage now includes `indexed_documents` with stable refs for local and Drive records,
`total_indexed_documents`, `next_document_offset`, and sanitized `local_sources` health.
The existing issue pagination remains separate. Reader MCP coverage redacts failing filenames,
paths and detailed reasons (fields are null); admin/reviewer inspection retains details.
Reader doctor exposes a generic sync error. Use aggregate coverage to disclose incomplete results.

Local document IDs are allocated monotonically after upgrade; deleting a document does not
allow its stable ref to resolve to an unrelated later document. Existing IDs are preserved.

### XLSX compatibility diagnostics (0.10.1)

The extractor retries only openpyxl's exact `Colors must be aRGB hex values` failure. It removes
one leading `#` from six/eight-digit hexadecimal `rgb` attributes in SpreadsheetML style elements
in a private temporary copy. Source workbooks are not changed. Metadata/search expose
`compatibility_repairs` (`kind=xlsx_rgb_hash_prefix`, `part=xl/styles.xml`, `count=N`).

Operator errors use `file '<id-or-source-relative-path>': stage=<stage> error=<ExceptionType>`
with `code=<code>` for extraction errors. Codes: `xlsx_unsupported_color` (unsupported RGB),
`xlsx_stylesheet_unreadable` (unsafe/unreadable styles or failed retry), `xlsx_archive_limit`
(resource limit), and `xlsx_parse_failed` (other loading/iteration errors). Messages contain no
cell contents, absolute paths or underlying exception text. Embedding failures are separate
`embedding:` warnings. Reader views retain existing redaction.

Limits: 10,000 ZIP members, 16 MiB styles, 512 MiB total uncompressed data, 200:1 member compression
ratio. DTD/entity declarations, duplicate names and missing worksheet parts are rejected.
See [v0.10.1](releases/v0.10.1.md) for exact compatibility and upgrade/rollback rules.
