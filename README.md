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
  index.sqlite3               metadata/relationship FTS5, vectors, reviews, revisions
  artifacts/extracted/        source-hash-addressed extracted text and provenance
  artifacts/revisions/        additive human-corrected PDF text revisions
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
local-rag reindex --target report.pdf  # rebuild chunks/FTS from cached extracted text
local-rag rebuild --all                # rebuild all index state from cached artifacts
local-rag reindex --target report.pdf --reextract  # deliberately rerun extraction/OCR
local-rag serve                        # continuous watch + periodic reconciliation
```

Create, modify, delete, rename, and move events use Watchdog's native platform observer. Irrelevant
events are filtered and a bounded queue coalesces changes until writes stabilize. A full
reconciliation runs every 60 seconds by default to recover missed events. Reconciliation reads one
document snapshot, trusts matching size/mtime without hashing, and hashes only candidates. If a
candidate's content hash is unchanged, its stored stat metadata is refreshed without extraction.
Extraction and chunk preparation finish before a short SQLite transaction replaces index state.
Default `reindex`/`rebuild` reconstructs chunks and FTS from the effective corrected artifact, or
the base extracted artifact when there is no correction. Unlike normal reconciliation, deliberate
reindex hashes every targeted source before trusting its cached artifact. It does not rerun
extraction/OCR when that hash matches, and preserves review status, corrections, and revision
history. Use the explicit `--reextract` option only when fresh extraction/OCR is intended; it
replaces derived extraction and review state from the authoritative source. PDF review items and
corrections survive restarts; source files are never modified.

Supported extraction and provenance:

- TXT and Markdown: line numbers.
- DOCX: paragraph and table/row/column positions.
- XLSX: sheet names and cell coordinates; formulas use cached displayed values.
- PPTX: slide and shape numbers.
- PDF: page numbers and native/OCR provenance from `firecrawl/pdf-inspector==1.15.0`.

Extracted artifacts are keyed by SHA-256 source hash. Unchanged files reuse artifacts, chunks, and
vectors. Missing unique chunk embeddings are collected after FTS indexing and submitted in batches
of at most 128. Vector cache keys combine chunk hash, provider, and model. Provider failures produce
warnings rather than rolling back working FTS content.

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
local-rag review correct 12 "Corrected searchable page text" \
  '[{"path":"scan.pdf","locator":"page:4","quote":"checked against scan"}]' \
  --actor reviewer-name
```

A correction creates an additive revision artifact with evidence and actor, resolves the review,
and rebuilds only that document's chunks. The base OCR artifact is retained and the unchanged PDF
is not OCRed again. Default reindex also keeps the effective corrected artifact and its complete
review/revision state.

## Search and embeddings

Search is global unless `--scope` is supplied:

```bash
local-rag search "renewal clause"
local-rag search "renewal clause" --scope legal/contracts
local-rag search "renewal clause" --mode full_text
local-rag search "renewal clause" --mode semantic
local-rag read legal/contracts/example.docx
```

Modes are `full_text`, `semantic`, and `hybrid` (default). Hybrid reports its effective mode and
falls back to full text with a warning if embedding inference is unavailable. Semantic-only fails
clearly when its provider or cached corpus vectors are unavailable. Scope is absent for global
search; literal `%`, `_`, and backslashes in subfolder names are escaped. Results include concise
mode, warning, match-source, and provenance information.

FTS5 is always local. Automatic metadata, agent metadata, and relationships share an efficient
local metadata FTS index. Embeddings are optional and vectors always remain in SQLite. Local models
load lazily; long-lived services retain a bounded 128-query embedding cache.

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

The project never enables a paid provider implicitly. Remote/local provider failure cannot make
full-text indexing or search unavailable.

## Security and privacy

The source folder remains authoritative, but the SQLite database and extracted/revision artifacts
contain searchable copies of document content and metadata. Keep `~/.local-rag` readable only by
trusted local users and include it in backup/retention decisions appropriate for the source data.

Local OCR and local embeddings do not send document text to an inference API. Enabling the
OpenAI-compatible embedding provider sends indexed chunk text and search queries to the configured
endpoint. Review that provider's data policy before enabling it, keep API keys in environment
variables, and never place credentials in `config.json`, MCP examples, commits, or issue reports.

MCP uses stdio and its reader/reviewer/admin modes are capability exposure boundaries, not user
authentication. Run reviewer/admin mode only for trusted local agents. See
[`SECURITY.md`](SECURITY.md) for vulnerability reporting and additional deployment guidance.

## Evidence-backed metadata and relationships

Automatic metadata (hash, size, title, type, word count, source-position count, and extractor
details) is stored during indexing. Agents/operators can add facts and relationships only with a
non-empty evidence array. Each evidence item must name an indexed path and include a locator or
quote.
Automatic and agent-added values plus relationship types/paths are searchable in `full_text` and
`hybrid` modes.

```bash
local-rag metadata add report.md status '"approved"' \
  '[{"path":"report.md","locator":"line:8","quote":"Approved"}]'
local-rag metadata get report.md
local-rag relationship report.md appendix.xlsx supports \
  '[{"path":"report.md","locator":"line:12"}]'
```

## MCP

Adapt [`config/mcp.json.example`](config/mcp.json.example), or run `local-rag mcp`. The MCP server
namespace supplies `local-rag`, so tool names stay short. Stdio uses newline-delimited JSON-RPC.
Default `reader` mode exposes only:

- `search`, `read`, `status`, `metadata`, `reviews`

Explicit reviewer mode adds mutations:

- `local-rag mcp --mode reviewer`
- `correct_page`, `resolve_review`, `add_metadata`, `add_relationship`

Explicit admin mode adds `reconcile` and `reindex`:

```bash
local-rag mcp --mode admin
# or set LOCAL_RAG_MCP_MODE=admin for local-rag-mcp
```

Mutation targets/scopes are still resolved against the single configured root.
Admin `reindex` accepts `reextract: false` by default. Set it to `true` only to deliberately rerun
extraction/OCR rather than rebuilding from cached effective/base artifacts.

## Verification

```bash
pip install -e '.[dev]'
python -m unittest discover -s tests -v
ruff format --check src tests
ruff check src tests
python -m compileall -q src tests
```

The tests cover configuration containment, checksum-verified runtime installation, TXT/Markdown/
DOCX/XLSX/PPTX/PDF extraction, provenance, fast stat reconciliation, bounded batch embeddings,
provider fallback, all search modes, literal-safe global/scoped search, indexed metadata and
relationships, additive OCR correction, native watcher selection/coalescing, deletion, and MCP mode
exposure/tool calls. Reindex regressions prove cached rebuilds do not call extractors, corrected PDF
text and revision state survive, and explicit re-extraction calls the extractor. A 1,000-file
regression proves no hashing on unchanged files and at most eight embedding calls for 128-item
batches.

## Remaining design boundaries

Vectors are JSON arrays scored in process, appropriate for a personal/local corpus rather than a
multi-million-chunk deployment. The explicit reviewer/admin MCP modes are an exposure boundary, not
authentication. The service has no rich Office chart/image OCR, semantic reranker, or GPU runtime
manager. Intel macOS is not supported by the pinned ONNX Runtime release used for the reproducible
OCR path.
