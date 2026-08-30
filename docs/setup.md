# Operator setup

Choose one explicit mode:

```bash
local-rag-mcp setup --full
local-rag-mcp setup --no-ocr
local-rag-mcp doctor --json
```

`--full` downloads checksum-pinned PDFium and ONNX Runtime artifacts, warms the pinned OCR model
cache, and verifies a second OCR pass offline. It requires network access during provisioning only.
`--no-ocr` is a supported deployment: text, Markdown, Office documents, native PDF text, FTS,
metadata, and relationships continue to work; pages classified as needing OCR enter durable review.

Then add at least one source and reconcile:

```bash
local-rag-mcp source add-local notes /absolute/path/to/notes
local-rag-mcp reconcile
```

For Drive, create the read-only token locally, then register one root and account label:

```bash
local-rag-mcp auth-google \
  --client-secret /secure/google-desktop-client.json \
  --token-file ~/.local-rag/credentials/work.json
local-rag-mcp source add-drive work-drive GOOGLE_DRIVE_FOLDER_ID \
  --account work@example.com \
  --token-file ~/.local-rag/credentials/work.json
local-rag-mcp sync --source work-drive --full
```

Do not paste credential contents into an agent conversation. Source and credential provisioning is
CLI-only; MCP intentionally has no add-source or OAuth tools. `doctor --json` checks SQLite/FTS,
enabled sources, OCR, embedding availability, reviews, and sync errors. A blocked check exits 2;
degraded optional capabilities still report usable FTS explicitly.

Embeddings are optional. Configure local inference with:

```bash
python -m pip install 'local-rag-mcp[local-embeddings]'
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=local
export LOCAL_RAG_MCP_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
local-rag-mcp doctor --json
local-rag-mcp reindex --all
```

Or configure an OpenAI-compatible provider such as OpenRouter:

```bash
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=openai
export LOCAL_RAG_MCP_EMBEDDING_MODEL=provider/model-id
export LOCAL_RAG_MCP_OPENAI_BASE_URL=https://openrouter.ai/api/v1
export LOCAL_RAG_MCP_OPENAI_API_KEY='set-this-outside-the-repository'
local-rag-mcp doctor --json
local-rag-mcp reindex --all
```

Without embeddings, use `full_text`; hybrid degrades to it with a warning, while semantic-only
fails clearly. A configured remote provider is reported as configured but is not contacted by
`doctor`.

Register MCP using the installed stdio executable and the same data root:

```json
{
  "mcpServers": {
    "local-rag": {
      "command": "local-rag-mcp-server",
      "env": {
        "LOCAL_RAG_MCP_HOME": "~/.local-rag",
        "LOCAL_RAG_MCP_PROFILE": "reader"
      }
    }
  }
}
```

Restart the MCP host, then call `doctor`, `sources`, `search`, and `read`. Use `reviewer` or `admin`
only for trusted local agents that need the additional mutation tools.

## Optional continuous indexing

Manual `reconcile`, `reindex`, `--reextract`, CLI, and MCP operation never depend on a background
service. To opt in on macOS LaunchAgent or Linux systemd user services:

```bash
local-rag-mcp service install
local-rag-mcp service start
local-rag-mcp service status
local-rag-mcp service stop
local-rag-mcp service uninstall
```

Install writes a user unit, but setup never installs or starts it. The long-lived process uses
native filesystem events for fast local updates. Every 600 seconds it reconciles all enabled
sources, recovering missed local events and incrementally polling remote change cursors. Override
the interval at setup with `--reconcile-seconds`.

Service templates deliberately do not copy embedding API keys or OAuth tokens. Local embeddings
work from the saved provider/model configuration. Remote embeddings require the service process to
receive `LOCAL_RAG_MCP_OPENAI_API_KEY` and related provider variables through an operator-managed,
owner-readable wrapper or platform credential/environment facility; interactive shell exports are
not automatically inherited by LaunchAgent or systemd. Without those credentials, indexing still
commits FTS and reports embeddings unavailable. Keep secret values out of unit files and logs.

Indexing is serialized through durable jobs:

```bash
local-rag-mcp reconcile
local-rag-mcp reconcile --source notes --background
local-rag-mcp reindex --source notes --target folder
local-rag-mcp reindex --source notes --target folder/file.pdf --reextract
local-rag-mcp jobs list
local-rag-mcp jobs status JOB_ID
```

Jobs record phases, heartbeat, aggregate progress, and completion/error state. Search/read use
committed SQLite WAL state throughout indexing. Conflicting writers are rejected; identical active
work coalesces to the same job ID.
