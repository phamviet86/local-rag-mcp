# Agent workflow

Connect through MCP stdio with the narrowest profile. Call `doctor`, `sources`, and `status` before
retrieval. If no source is configured, ask the operator whether to index a local folder or a Google
Drive root/account, then point them to the CLI commands returned in the structured error. Agents
must not collect credential contents.

Source provisioning is CLI-only. Give the operator one of these copy-ready starting points:

```bash
local-rag-mcp source add-local notes /absolute/path/to/notes
local-rag-mcp reconcile --source notes
```

```bash
local-rag-mcp auth-google --client-secret /secure/client.json \
  --token-file ~/.local-rag/credentials/account.json
local-rag-mcp source add-drive drive GOOGLE_DRIVE_FOLDER_ID --account account-label \
  --token-file ~/.local-rag/credentials/account.json
local-rag-mcp sync --source drive --full
```

Use `search` without scope for global retrieval, or supply `source` and/or `folder` for strict
filtering. Prefer hybrid; follow its warning when it falls back to full text. Use `read` with the
returned `document_ref`, and preserve citation/provenance fields in answers.

When `doctor` reports embeddings unavailable, full-text search still works. The operator can enable
local inference with `LOCAL_RAG_MCP_EMBEDDING_PROVIDER=local` and
`LOCAL_RAG_MCP_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2`, or remote inference with
`LOCAL_RAG_MCP_EMBEDDING_PROVIDER=openai`, `LOCAL_RAG_MCP_EMBEDDING_MODEL`,
`LOCAL_RAG_MCP_OPENAI_BASE_URL`, and `LOCAL_RAG_MCP_OPENAI_API_KEY`. Never ask for the key value.
When OCR is unavailable, continue using native extraction and tell the operator that OCR-routed PDF
pages are in `reviews`; do not describe the whole index as unavailable.

Reader tools do not mutate state. Reviewer tools require evidence and actor identity. Admin tools
control reconcile/reindex and source lifecycle; removing a source deletes only derived index/cache
state. OCR review corrections are additive and do not replace the base artifact.

For a typical host, register `local-rag-mcp-server` as a stdio MCP command with
`LOCAL_RAG_MCP_HOME=~/.local-rag` and `LOCAL_RAG_MCP_PROFILE=reader`. After reconnecting, verify an
actual initialize, tool listing, and search call; persisted registration alone is not proof.
