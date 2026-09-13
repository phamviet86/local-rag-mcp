# Conditional integrations

Use only the section required by the operator's chosen source/provider. In commands below, `cli`
and `python` mean the absolute executable paths in the generated `runtime.json`; include the same
`--home DATA_HOME` before CLI subcommands.

## Install an optional extra

Use `package_source` from `runtime.json` when it still exists and identifies the selected checkout
or wheel. In the same isolated Python environment, install
`phamviet-local-rag-mcp[google-drive] @ PACKAGE_SOURCE` or
`phamviet-local-rag-mcp[local-embeddings] @ PACKAGE_SOURCE` with `python -m pip install`.
This retains the actual installation version/channel; do not substitute an older release or a
different checkout. If the original source is unavailable, locate the operator's intended wheel
or checkout before changing the package. Never install the unrelated PyPI `local-rag-mcp` project.

## Google Drive

Required: folder/root ID, an account label, and a usable read-only OAuth token file. Reuse existing
authorized configuration. Normalize a supplied Drive folder URL to its ID; use `--shared-drive-id`
when a Shared Drive requires it. A folder ID or API key alone does not authorize Drive access.

For first authorization, the operator needs a Google Cloud project with Drive API enabled and an
OAuth **Desktop app** client JSON file. If absent, explain these console steps and ask for the local
client-file path after creation; do not ask for its contents. The CLI opens the browser consent flow:

```text
cli auth-google --client-secret CLIENT_JSON_PATH --token-file DATA_HOME/credentials/work.json
cli source add-drive NAME FOLDER_ID --account ACCOUNT --token-file DATA_HOME/credentials/work.json
cli sync --source NAME --full --background
cli jobs status JOB_ID
```

The requested scope is `https://www.googleapis.com/auth/drive.readonly`. Token files are owner-only;
never relay their contents. OAuth provisioning is a CLI/operator action, not an MCP tool. Existing
folder/account choices and authorization need not be reconfirmed. If consent is required, continue
independent configuration while waiting and resume with the existing source/job state.

## Embeddings and persistent credentials

Full-text search needs no provider or API key. Preserve it when embeddings are absent. Enable local
or remote embeddings only when selected; a remote endpoint receives indexed chunks and queries.

The CLI, MCP server, and optional service all read `DATA_HOME/credentials/embeddings.json` when it
exists. An explicit `LOCAL_RAG_MCP_EMBEDDING_CONFIG` can select another file; otherwise matching the
data home is sufficient across GUI launches and reboots. The file must be an owner-owned regular
file with mode `0600` and should be under a `0700` credentials directory. Symlinks are rejected.
Recognized environment variables override matching file settings. Never display the file or dump
the environment to inspect whether a key exists.

For a local model, install the `local-embeddings` extra and create the protected JSON using the
operator's chosen model (the model may download on first use):

```json
{"embedding_provider":"local","embedding_model":"sentence-transformers/all-MiniLM-L6-v2"}
```

For a remote OpenAI-compatible provider, the nonsecret configuration fields are:

```json
{"embedding_provider":"openai","embedding_model":"MODEL_ID","openai_base_url":"HTTPS_ENDPOINT","openai_api_key":""}
```

Create the file with restrictive permissions before writing. Populate an existing operator-approved
key directly from its protected local source/environment without printing it. If no key is available,
prepare all other configuration and provide a local masked-input step (`getpass` in an interactive
terminal or the operator's secure editor) to fill only `openai_api_key`; do not ask for chat input or
place values in process arguments/history. Do not overwrite an existing credential file wholesale.
If the agent cannot open an interactive terminal, give the operator the exact file location and
local entry step; pause only provider-dependent work until it is complete.

Run `doctor --json`, then the authorized `reindex --all --background` when changing the embedding
model/provider requires vectors for existing documents. Poll the job. `doctor` does not call the
remote provider: verify a real semantic query after indexing and report failures honestly. Test from
a fresh process without temporary shell exports to establish that the protected file is used. Never
copy keys into Codex MCP configuration, skill assets, service definitions, reports, or logs.
