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
