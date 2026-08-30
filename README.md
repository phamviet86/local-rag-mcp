# local-rag-mcp

`local-rag-mcp` is a local-first, multi-source document index for agents. Local folders and Google
Drive remain authoritative; one shared SQLite database stores FTS5, vector caches, metadata,
relationships, OCR reviews, and source state. Extracted text and model/runtime caches stay below
`~/.local-rag` by default.

The project is pre-1.0 and currently has no selected license. Python 3.11+ and SQLite with FTS5 are
required.

## Install and set up

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

local-rag-mcp setup --full    # provision and verify pinned local OCR
# or: local-rag-mcp setup --no-ocr
local-rag-mcp source add-local work /absolute/path/to/documents
local-rag-mcp reconcile
local-rag-mcp doctor --json
```

Setup recommends—but never requires—the optional auto-index service. Manual CLI and MCP indexing
remain fully supported without it. The service keeps native local filesystem watchers active and
performs incremental reconciliation of all local and remote sources every 600 seconds as recovery.

```bash
local-rag-mcp service install
local-rag-mcp service start
local-rag-mcp service status
```

`setup --no-ocr` remains fully usable for native extraction and FTS; OCR-routed pages are queued for
review. Missing embeddings similarly leaves FTS available. See [`docs/setup.md`](docs/setup.md) for
operator setup and [`docs/agents.md`](docs/agents.md) for agent behavior.

`init /existing/root` is a no-OCR compatibility shortcut that registers the old single local
root as source `default`. Otherwise add any number of sources after `init`.

The data layout is:

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

## Compatibility with local-rag

The distribution, product, primary CLI, MCP server, and public namespace are now `local-rag-mcp`.
Compatibility is explicit:

- `local-rag` remains an installed CLI alias.
- Existing storage and the legacy CLI remain supported.
- `~/.local-rag`, `LOCAL_RAG_HOME`, and existing `LOCAL_RAG_*` embedding variables remain valid.
- Schema migration v3 registers an existing one-root index as source `default` in place. It does not
  move source files, artifacts, reviews, revisions, metadata, or vectors.

New deployments should use `LOCAL_RAG_MCP_HOME` and `LOCAL_RAG_MCP_*` variables.

`local-rag-mcp` is a standalone service, not an embeddable agent library. Agents and operators must
integrate through the documented CLI or MCP server. Python modules under `local_rag` are internal
implementation details, and `local_rag_mcp` intentionally exports only its version marker.

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

Removing a source deletes only that source's database rows, unreferenced extracted/revision
artifacts, orphan vectors, and source-specific downloaded cache. It never deletes or modifies local
files or Drive items. Shared content-hash artifacts and vectors remain while another source
references them.

### Google Drive roots and accounts

Each Drive source has its own root folder ID, account label, OAuth token path, change cursor, and
optional shared-drive ID. Create a token with the read-only Drive scope:

```bash
local-rag-mcp auth-google \
  --client-secret /secure/google-desktop-client.json \
  --token-file ~/.local-rag/credentials/work-account.json

local-rag-mcp source add-drive work-drive DRIVE_ROOT_FOLDER_ID \
  --account work@example.com \
  --token-file ~/.local-rag/credentials/work-account.json
```

Client secrets and token contents are never stored in SQLite; only the token path is registered, and
that path is omitted from reader/status output.
Token files must have mode `0600` on POSIX. Full sync walks the configured root, rejects incomplete
Drive listings and unsafe path segments, and supports text/Markdown, PDF, DOCX, XLSX, PPTX, Google
Docs, Sheets, and Slides. Incremental sync consumes the Drive changes cursor, resolves moves against
an authoritative bounded tree listing, and downloads/extracts only changed content fingerprints. If
any changed item fails download, extraction, or indexing, the durable cursor is not advanced, so the
entire change page is safely retried.

## Reconcile, watch, reindex, and recovery

```bash
local-rag-mcp reconcile                         # every enabled source
local-rag-mcp reconcile --source engineering --background
local-rag-mcp sync --source work-drive
local-rag-mcp sync --source work-drive --full
local-rag-mcp reconcile reports --source engineering
local-rag-mcp serve                             # native local watchers + periodic all-source sync

local-rag-mcp reindex --source engineering
local-rag-mcp reindex --source engineering --target reports/2026
local-rag-mcp reindex --source work-drive --target team/handbook
local-rag-mcp reindex --all
local-rag-mcp reindex --source work-drive --reextract
local-rag-mcp jobs list
local-rag-mcp jobs status JOB_ID
```

Indexing commands use a durable single-writer queue. Their JSON includes a job ID, phase,
heartbeat, discovered/processed/searchable/remaining counts, and embedding-pending count. A second
identical job coalesces; a conflicting job is rejected. SQLite WAL readers and MCP search/read stay
available from the last committed index while a job runs.

Normal local reconciliation uses size/mtime as its unchanged fast path and hashes candidates.
Targets are relative file or folder paths within either kind of source; partial Drive rebuilds do
not advance the source-wide changes cursor. Deliberate local reindex hashes every targeted file.
When hashes match, local and Drive reindex
rebuild chunks/FTS from the effective corrected or base cached artifact without rerunning extraction
or OCR. `--reextract` is the explicit opt-in to rerun extraction/OCR. Provider failures never roll
back usable FTS state.

Native Watchdog observers coalesce writes for all enabled local roots. Periodic reconciliation also
syncs Drive and recovers missed filesystem/change events.

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
reciprocal-rank fusion of the ordered BM25 and semantic result lists, avoiding incompatible raw-score
mixing and BM25 sign inversions. If query embeddings fail, hybrid returns FTS results with a warning;
semantic-only fails clearly.

Folder scopes are literal safe relative paths. Leading or trailing dots are preserved, so names such
as `.hidden` and `reports.` work normally. Supplying both source and folder applies both filters
strictly; omitting them searches every enabled source.

Each result includes `source`, `source_kind`, an unambiguous `document_ref`, match provenance, and a
citation containing source, external ID, authoritative URL when available, relative path, content
hash, PDF page, and source locators. Automatic metadata, evidence-backed agent metadata, and document
relationships share the local metadata FTS index.

The returned `document_ref` (`source:relative/path`) can be passed directly to `read` or `metadata`.
`read` returns source name/kind, external ID, URL, source revision and content hash, indexed time,
authority, and the extracted source-position provenance alongside the cached text window.

Vectors remain in SQLite and are cached by chunk hash plus a fingerprint of provider, model,
endpoint, and declared dimensions. Missing unique embeddings are batched; local models load lazily;
query embeddings use a bounded in-process cache.

```bash
# Local inference
python -m pip install -e '.[local-embeddings]'
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=local
export LOCAL_RAG_MCP_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2

# OpenAI-compatible inference, including OpenRouter
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=openai
export LOCAL_RAG_MCP_EMBEDDING_MODEL=provider/model
export LOCAL_RAG_MCP_OPENAI_BASE_URL=https://openrouter.ai/api/v1
export LOCAL_RAG_MCP_OPENAI_API_KEY='...'
```

Remote inference is never enabled implicitly. Enabling it sends indexed chunks and search queries to
the configured endpoint; vectors remain local.

## Extraction, OCR review, and evidence

Local and downloaded Drive files share the same content-hash extraction cache. TXT/Markdown,
DOCX, XLSX, and PPTX provenance records line, paragraph/table cell, sheet/cell, and slide/shape
positions. PDF inspection/native extraction uses `firecrawl/pdf-inspector==1.15.0`; only routed pages
use the pinned, checksum-verified local PDFium/ONNX/PP-OCRv6 runtime.

Blank, failed, complex, and low-quality pages enter a durable review queue:

```bash
local-rag-mcp ocr install
local-rag-mcp review list
local-rag-mcp review correct 12 "Corrected page text" \
  '[{"path":"legal-drive:scan.pdf","locator":"page:4","quote":"checked scan"}]' \
  --actor reviewer
```

Corrections create additive evidence/actor revisions, keep the base OCR artifact, and rebuild only
the affected searchable document. Default reindex preserves the effective correction, review status,
and revision history.

Metadata and relationships require non-empty evidence:

```bash
local-rag-mcp metadata add report.md status '"approved"' \
  '[{"path":"engineering:report.md","locator":"line:8","quote":"Approved"}]' \
  --source engineering

local-rag-mcp relationship report.md appendix.xlsx supports \
  '[{"path":"engineering:report.md","locator":"line:12"}]' \
  --source-source engineering --target-source finance
```

## MCP SDK server

The server uses the official Python MCP SDK over stdio. Adapt
[`config/mcp.json.example`](config/mcp.json.example), then select the narrowest profile:

```bash
LOCAL_RAG_MCP_PROFILE=reader local-rag-mcp-server
LOCAL_RAG_MCP_PROFILE=reviewer local-rag-mcp-server
LOCAL_RAG_MCP_PROFILE=admin local-rag-mcp-server
```

- `reader`: `search`, `read`, `status`, `doctor`, `index_status`, `job_status`, `sources`,
  `metadata`, `reviews`.
- `reviewer`: reader tools plus page correction/review resolution and evidence-backed metadata and
  relationships.
- `admin`: reviewer tools plus queued reconcile/reindex starts, synchronous reconcile/reindex,
  enable/disable, and confirmed source removal.

SDK tool annotations mark read-only, mutating, and destructive operations. Profiles are capability
exposure boundaries, not authentication. Source removal requires explicit confirmation and still
cannot delete authoritative source files.

## Security and verification

Read [`SECURITY.md`](SECURITY.md). Protect the data root because SQLite and artifacts contain
searchable document copies. Reviewer/admin MCP profiles should be available only to trusted local
agents. Review remote embedding provider data policies before enabling them.

```bash
ruff format --check src tests
ruff check src tests
mypy
pytest
python -m compileall -q src tests
local-rag-mcp --help
local-rag-mcp mcp --help
```

CI uses read-only GitHub permissions, pinned actions, concurrency cancellation, Python 3.11-3.13,
format/lint/type/test checks, and CLI smoke tests.

## Current scale and operational boundaries

Vectors are JSON arrays scored in process, suitable for a personal/team corpus rather than millions
of chunks. SQLite remains the intentional shared local engine; no heavy vector database is added.
Drive change processing performs an authoritative tree metadata read to resolve moves safely. Live
Google OAuth/Drive behavior requires operator credentials and network access; mocked workflows cover
read-only OAuth token creation/permissions/redaction, full/incremental sync, duplicate IDs across
accounts, update/delete, targeted and cached rebuild, and purge.
