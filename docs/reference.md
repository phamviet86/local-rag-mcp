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

Full sync walks the configured root, rejects incomplete listings and unsafe path segments, and
supports text/Markdown, PDF, DOCX, XLSX, PPTX, Google Docs, Sheets, and Slides. Incremental sync
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
```

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
PDF inspection/native extraction uses `firecrawl/pdf-inspector==1.15.0`; only routed pages use the
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

The server uses the official Python MCP SDK over stdio. Select the narrowest profile:

```bash
LOCAL_RAG_MCP_PROFILE=reader local-rag-mcp-server
LOCAL_RAG_MCP_PROFILE=reviewer local-rag-mcp-server
LOCAL_RAG_MCP_PROFILE=admin local-rag-mcp-server
```

- `reader`: `search`, `read`, `status`, `doctor`, `index_status`, `job_status`, `sources`,
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
