# local-rag

`local-rag` is a local-first document index and MCP service. One configured folder is the source of
truth; SQLite, extracted text, OCR models, and caches are rebuildable local derivatives.

## Install and initialize

Python 3.9+ and SQLite FTS5 are required.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

local-rag init /absolute/path/to/documents
local-rag scan
local-rag status
local-rag search "quarterly decision"
```

The default data root is `~/.local-rag` on macOS, Linux, and Windows. Override it with global
`--home PATH` or `LOCAL_RAG_HOME`. It contains:

```text
~/.local-rag/
  config.json                 one recursive source root and exclusions
  index.sqlite3               metadata, FTS5, vectors, jobs, reviews, relationships
  artifacts/extracted/        source-hash-addressed extracted text and provenance
  models/                     local OCR and embedding models
  cache/                      local inference caches
  runtime/                    pinned PDFium and ONNX Runtime libraries
```

Standard exclusions include Git/virtual-environment metadata, `node_modules`, and Python caches.
Additional directory names can be supplied during `init` with repeated `--exclude NAME`. Directory
symlinks are not traversed, and resolved file symlinks must remain inside the configured root.

## Indexing and watching

```bash
local-rag reconcile                    # complete root
local-rag reconcile reports/2026       # one subfolder
local-rag reindex --target report.pdf  # force re-extraction
local-rag rebuild --all                # force complete rebuild
local-rag serve                        # continuous watch + periodic reconciliation
```

Create, modify, delete, rename, and move events are handled by a cross-platform polling observer.
A full reconciliation runs every 60 seconds by default to recover missed events. Extraction and
chunk preparation finish before a short SQLite transaction replaces the previous index state.
Jobs and PDF review items survive restarts; source files are never modified.

Supported extraction and provenance:

- TXT and Markdown: line numbers.
- DOCX: paragraph and table/row/column positions.
- XLSX: sheet names and cell coordinates; formulas use cached displayed values.
- PPTX: slide and shape numbers.
- PDF: page numbers and native/OCR provenance from `firecrawl/pdf-inspector==1.15.0`.

Extracted artifacts are keyed by SHA-256 source hash. Unchanged files reuse artifacts, chunks, and
vectors. Vector cache keys combine chunk hash, provider, and model.

## Local PDF OCR

PDF classification and native extraction use Firecrawl's Rust `pdf-inspector`. Install the local
selective-OCR runtime once:

```bash
local-rag ocr install
```

The installer selects the current supported platform and verifies hard-coded SHA-256 checksums for
Firecrawl PDFium `native-v7988` and ONNX Runtime `1.27.0`. Linux x64/ARM64, macOS Apple Silicon, and
Windows x64 are supported. On the first routed page, `pdf-inspector` downloads and verifies its
pinned PP-OCRv6 Small `oar-ocr-v0.7.0` artifacts below the local model directory. OCR runs locally;
no Apple Vision or cloud OCR is used.

Only pages classified for OCR are sent through the OCR engine. Native text is retained when useful,
OCR replaces/fuses routed pages, blank results are omitted, and failed, low-quality, or complex
pages enter the durable queue:

```bash
local-rag review list
local-rag review resolve 12 "Checked against the source scan"
```

## Search and embeddings

Search is global unless `--scope` is supplied:

```bash
local-rag search "renewal clause"
local-rag search "renewal clause" --scope legal/contracts
local-rag read legal/contracts/example.docx
```

FTS5 is always local. Embeddings are optional and vectors always remain in SQLite.

Local inference (downloads the selected model into the local cache):

```bash
pip install -e '.[local-embeddings]'
export LOCAL_RAG_EMBEDDING_PROVIDER=local
export LOCAL_RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

OpenAI-compatible inference, including OpenRouter, is opt-in and fails closed without credentials:

```bash
export LOCAL_RAG_EMBEDDING_PROVIDER=openai
export LOCAL_RAG_EMBEDDING_MODEL=provider/model
export LOCAL_RAG_OPENAI_BASE_URL=https://openrouter.ai/api/v1
export LOCAL_RAG_OPENAI_API_KEY='...'
```

The project never enables a paid provider implicitly.

## Evidence-backed metadata and relationships

Automatic metadata (hash, size, title, type, word count, source-position count, and extractor
details) is stored during indexing. Agents/operators can add facts and relationships only with a
non-empty evidence array. Each evidence item must name an indexed path and include a locator or
quote.

```bash
local-rag metadata add report.md status '"approved"' \
  '[{"path":"report.md","locator":"line:8","quote":"Approved"}]'
local-rag metadata get report.md
local-rag relationship report.md appendix.xlsx supports \
  '[{"path":"report.md","locator":"line:12"}]'
```

## MCP

Adapt [`config/mcp.json.example`](config/mcp.json.example), or run `local-rag mcp`. The stdio server
uses newline-delimited JSON-RPC and exposes clearly separated tools:

Read operations:

- `local_rag_search`, `local_rag_read`, `local_rag_status`
- `local_rag_review_list`, `local_rag_metadata_get`

Administrative mutations:

- `local_rag_admin_reconcile`, `local_rag_admin_reindex`
- `local_rag_admin_review_resolve`
- `local_rag_admin_metadata_add`, `local_rag_admin_relationship_add`

Administrative target/scope values are resolved against the single configured root.

## Verification

```bash
pip install -e '.[dev]'
python -m unittest discover -s tests -v
ruff format --check src tests
ruff check src tests
python -m compileall -q src tests
```

The tests cover configuration containment, checksum-verified runtime installation, TXT/Markdown/
DOCX/XLSX/PPTX/PDF extraction, provenance, incremental and forced rebuilds, vector caching,
global/scoped search, deletion, durable artifacts/reviews, evidence validation, real watcher events,
and MCP initialization/tool calls.

## Remaining design boundaries

Vectors are JSON arrays scored in process, appropriate for a personal/local corpus rather than a
multi-million-chunk deployment. The service has no authentication boundary around administrative
MCP tools, no rich Office chart/image OCR, no semantic reranker, and no GPU runtime manager. Intel
macOS is not supported by the pinned ONNX Runtime release used for the reproducible OCR path.
