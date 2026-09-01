# Agent workflow

`local-rag-mcp` is a local service for trusted agents. Integrate through its MCP server or CLI; its
Python modules are not a public agent API. This guide supplements the repository-level contribution
rules in [../AGENTS.md](../AGENTS.md).

## Connect safely

Use the narrowest MCP profile. A normal retrieval agent gets `reader`:

```bash
codex mcp add local-rag-mcp \
  --env LOCAL_RAG_MCP_HOME="$HOME/.local-rag" \
  --env LOCAL_RAG_MCP_PROFILE=reader \
  -- "$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server"
```

The configured executable must be an absolute path belonging to the intended isolated environment.
Profiles expose capabilities only; they are not authentication or a sandbox. Give `reviewer` and
`admin` only to trusted local processes with operator approval.

## Retrieval sequence

1. Start with `doctor`, `status`, and `sources`.
2. If sources are enabled, use global `search` or strict `source`/`folder` filters.
3. Use the returned `document_ref` in `read`.
4. Preserve returned citation/provenance fields: source, relative path, URL where present, content
   hash/revision, page, and locator.

Prefer hybrid retrieval if embeddings are available. If it reports fallback, use the full-text
results honestly. When embeddings are unavailable, full-text retrieval remains usable; do not claim
the entire index is unavailable. When OCR is unavailable, native extraction remains usable and
OCR-routed PDF pages are held in the review queue.

## Empty state is normal

An operator may intentionally install the service with zero sources. `no_enabled_sources` means no
source has been enabled; it is not an initialization or MCP failure. Ask the operator whether they
want to register a local folder or a specific Google Drive root, then give one copy-ready CLI command:

```bash
local-rag-mcp source add-local notes /absolute/path/to/notes
local-rag-mcp reconcile --source notes
```

For Drive, direct the operator to [setup.md](setup.md). Do not request, collect, echo, store, or
transmit OAuth client secrets, OAuth tokens, remote embedding API keys, document contents, or source
paths outside the configured local service.

## Mutations and reviews

Reader tools do not mutate state. Reviewer actions require evidence plus actor identity. Admin
actions can enqueue/reconcile/reindex and manage source lifecycle; source removal only purges derived
local state and never authoritative source files.

Index writes use a durable single-writer queue. Reader tools can inspect job/index status and search
committed state while work runs. Admin agents should poll `job_status`; duplicate active work may
coalesce and conflicting writes can be rejected.

The optional OS user service is an operator-controlled mechanism. Agents must never install,
start, stop, or uninstall it without explicit operator direction. It does not inherit interactive
remote-embedding credentials by default.
