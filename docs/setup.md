# Operator setup

This guide configures a single trusted macOS or Linux user installation. For the package source,
production virtual environment, upgrades, and rollback, read [deployment.md](deployment.md) first.

The product and commands are `local-rag-mcp`, but its Python distribution is
`phamviet-local-rag-mcp`. Do not install the unrelated PyPI package named `local-rag-mcp`.

## Initialize the data root

The default data root is `~/.local-rag`. It is created with owner-only permissions on POSIX and
contains a database plus derived document text, so choose a protected local volume. To use a
different data root, set `LOCAL_RAG_MCP_HOME` before every CLI/MCP invocation or pass `--home` to
the CLI. Choose one absolute `DATA_ROOT` and keep it identical for CLI, the optional service, and
MCP registration; when using `--home`, put it before the subcommand.

Choose exactly one setup mode:

```bash
DATA_ROOT="$HOME/.local-rag"
export LOCAL_RAG_MCP_HOME="$DATA_ROOT"
local-rag-mcp --home "$DATA_ROOT" setup --no-ocr
# Or: provision checksum-pinned local OCR runtime/model, requiring network during setup.
local-rag-mcp --home "$DATA_ROOT" setup --full
local-rag-mcp --home "$DATA_ROOT" doctor --json
```

`--no-ocr` supports Markdown, text, DOCX, XLSX, PPTX, native PDF text, FTS5, metadata, and
relationships. PDF pages routed for OCR are retained in the review queue. `--full` downloads,
verifies, and locally tests the pinned OCR assets; it does not start a service or add a source.

A doctor result with `0` sources/documents is a valid empty state, but readiness remains blocked.
`status` and `doctor` exit with code `2`, while retrieval and reconciliation return
`no_enabled_sources`; add a source only after the operator identifies its intended location.

## Add a local source

The root must be an absolute, accessible directory. The source files are never copied back to or
modified by this command. Directory symlinks are not traversed; file symlinks must resolve inside
the registered root.

```bash
local-rag-mcp source add-local notes /absolute/path/to/notes
local-rag-mcp reconcile --source notes
local-rag-mcp source list
local-rag-mcp search "renewal clause" --source notes --mode full_text
```

Use a stable source name; it becomes part of each document reference and citation. To exclude
additional directory names, repeat `--exclude`:

```bash
local-rag-mcp source add-local engineering /srv/engineering --exclude archive --exclude vendor
```

## Add a Google Drive source (optional)

Install the `google-drive` extra from the v0.7.1 release wheel when it is needed. OAuth provisioning
happens only on the operator's machine and is intentionally unavailable through MCP.

Before running the commands below, the operator must use a Google Cloud project to enable the Google
Drive API, configure the OAuth consent screen, and create/download an OAuth **Desktop app** client.
Follow Google's [Drive Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
for those console steps and its [installed-app OAuth guidance](https://developers.google.com/identity/protocols/oauth2/native-app)
for the desktop flow. Request only the read-only Drive scope required by this project. Store the
downloaded client JSON and resulting token in a protected operator-controlled location; do not commit,
attach, paste, or relay their contents to an agent.

```bash
# v0.7.1 GitHub Release wheel
python -m pip install \
  "phamviet-local-rag-mcp[google-drive] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"

local-rag-mcp auth-google \
  --client-secret /secure/google-desktop-client.json \
  --token-file "$HOME/.local-rag/credentials/work.json"
local-rag-mcp source add-drive work-drive GOOGLE_DRIVE_FOLDER_ID \
  --account work@example.com \
  --token-file "$HOME/.local-rag/credentials/work.json"
local-rag-mcp sync --source work-drive --full
```

Use a client configured for the Google Drive read-only scope and keep its secret/token outside this
repository. Never paste their contents into chat, issue reports, unit files, or logs. The registry
stores a token path, not its contents; source/status responses redact the token path for reader
tools.

## Optional embeddings

Full-text search needs no embeddings. The default `hybrid` mode falls back to full text with a
warning if embeddings are unavailable, while `semantic` fails clearly. Local embedding models and
remote providers are both opt-in.

```bash
# v0.7.1 GitHub Release wheel
python -m pip install \
  "phamviet-local-rag-mcp[local-embeddings] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=local
export LOCAL_RAG_MCP_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
local-rag-mcp doctor --json
local-rag-mcp reindex --all
```

For an OpenAI-compatible endpoint, configure these only in an operator-controlled environment:

```bash
export LOCAL_RAG_MCP_EMBEDDING_PROVIDER=openai
export LOCAL_RAG_MCP_EMBEDDING_MODEL=provider/model-id
export LOCAL_RAG_MCP_OPENAI_BASE_URL=https://example.invalid/v1
export LOCAL_RAG_MCP_OPENAI_API_KEY='set-outside-the-repository'
local-rag-mcp doctor --json
local-rag-mcp reindex --all
```

Remote inference sends indexed chunks and search queries to that endpoint. `doctor` reports whether
the provider is configured, but does not contact it. Review the provider's data policy first, and do
not add actual secret values to `.env.example` or version control.

## Connect Codex MCP

Register the installed server executable—not `python`, a shell alias, or a relative path—with the
reader profile and matching data root:

```bash
codex mcp add local-rag-mcp \
  --env LOCAL_RAG_MCP_HOME="$DATA_ROOT" \
  --env LOCAL_RAG_MCP_PROFILE=reader \
  -- "$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server"
codex mcp get local-rag-mcp
```

Restart/reconnect Codex, then verify a real initialize, tool list, `doctor`, `sources`, and `search`
call. The expected empty-install response is `no_enabled_sources`, not a server failure. The JSON
template is [../config/mcp.json.example](../config/mcp.json.example).

`reader` is the safe default. `reviewer` can resolve OCR reviews and add evidence-backed metadata;
`admin` can start indexing and change source lifecycle. Profiles do not authenticate callers—only
trusted local processes should get write-capable profiles.

## Optional continuous indexing

Manual `reconcile`, `reindex`, CLI, and MCP retrieval do not need a background process. To opt into
native local watchers and periodic reconciliation, install one user service:

```bash
local-rag-mcp --home "$DATA_ROOT" service install
local-rag-mcp --home "$DATA_ROOT" service start
local-rag-mcp --home "$DATA_ROOT" service status
local-rag-mcp --home "$DATA_ROOT" service stop
local-rag-mcp --home "$DATA_ROOT" service uninstall
```

The service is a macOS LaunchAgent or Linux systemd user service. It watches enabled local roots and
reconciles enabled sources every 600 seconds by default. Generated service definitions deliberately
do not copy OAuth tokens or remote embedding API keys. If remote embeddings are required, the
operator must supply them through a protected platform mechanism; otherwise full-text indexing
remains supported.

## Day-two checks

```bash
local-rag-mcp --home "$DATA_ROOT" doctor --json
local-rag-mcp --home "$DATA_ROOT" status
local-rag-mcp --home "$DATA_ROOT" source list
local-rag-mcp --home "$DATA_ROOT" jobs list
local-rag-mcp --home "$DATA_ROOT" review list
```

Use `reindex --reextract` only when deliberately rerunning extraction/OCR. For failure recovery,
data-root backups, package upgrades, rollback, and removal, continue with
[deployment.md](deployment.md).
