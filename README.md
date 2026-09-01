# local-rag-mcp

`local-rag-mcp` is a local-first document index and stdio MCP server for trusted agents. It keeps
source folders and Google Drive authoritative, while a local SQLite/FTS5 database stores derived
text, metadata, citations, OCR reviews, and optional vector caches below `~/.local-rag`.

It is a standalone service: use its CLI or MCP server, not its internal Python modules.

## Release status and identity

This repository is preparing **v0.7.0** as a release candidate. It is currently private and has no
published GitHub Release or PyPI release. Do not present it as already published or install it from
an unverified third-party artifact.

| Purpose | Name |
| --- | --- |
| Repository, product, CLI, MCP server | `local-rag-mcp` |
| Python distribution | `phamviet-local-rag-mcp` |
| Legacy CLI alias | `local-rag` |

The PyPI project named `local-rag-mcp` belongs to an unrelated project. **Never run**
`pip install local-rag-mcp` or `pip install 'local-rag-mcp[...]'` for this software. Once a release
is explicitly published, install the unique distribution named `phamviet-local-rag-mcp` instead.

The project is licensed under [Apache-2.0](LICENSE). It is an independent community project and is
not affiliated with any similarly named PyPI project.

Python **3.11–3.13** and SQLite with FTS5 are supported. See the
[support matrix](docs/deployment.md#support-matrix) before deploying it to another machine.

## Install a published GitHub Release

There is no release asset yet. When a maintainer publishes one, download the wheel attached to that
GitHub Release and install it in a dedicated production virtual environment. Do not guess a release
URL or version; replace the example wheel path with the file you actually downloaded and verified.

```bash
mkdir -p "$HOME/.local/share/local-rag-mcp"
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install "$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
local-rag-mcp --help
```

Optional capabilities are explicit. For a GitHub Release wheel, use the unique distribution name
with a local `file://` wheel reference:

```bash
# Local embedding model support
python -m pip install "phamviet-local-rag-mcp[local-embeddings] @ file://$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"

# Google Drive source support
python -m pip install "phamviet-local-rag-mcp[google-drive] @ file://$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
```

After a matching version has been explicitly published to PyPI, the unique distribution name can be
used instead: `python -m pip install 'phamviet-local-rag-mcp[local-embeddings]'` or
`python -m pip install 'phamviet-local-rag-mcp[google-drive]'`. Do not substitute
`local-rag-mcp[...]` in either installation method.

For a checkout used to contribute code, follow the development instructions in
[AGENTS.md](AGENTS.md); editable installs are not the production deployment path.

## First-time operator setup

Choose one OCR mode, then verify the local state. `--no-ocr` is a fully supported starting point;
native text extraction and full-text search remain available.

```bash
local-rag-mcp setup --no-ocr
# Or, with network access to provision the checksum-verified local OCR runtime:
local-rag-mcp setup --full
local-rag-mcp doctor --json
```

An initialized install with zero sources/documents is a valid empty state, but it is not ready for
retrieval. `status` and `doctor` exit with code `2`, and retrieval reports `no_enabled_sources`,
until an operator deliberately registers a source. The service never assumes a folder, Google Drive
account, remote embedding endpoint, or background process.

```bash
local-rag-mcp source add-local notes /absolute/path/to/documents
local-rag-mcp reconcile --source notes
local-rag-mcp search "example" --mode full_text
```

See [operator setup](docs/setup.md) for local and Drive sources, and
[deployment](docs/deployment.md) for data migration, upgrades, rollback, and uninstall.

## Connect Codex through MCP

Use the reader profile by default. This command passes an absolute executable and absolute data-root
path to Codex; adjust the two paths if you chose a different installation directory.

```bash
codex mcp add local-rag-mcp \
  --env LOCAL_RAG_MCP_HOME="$HOME/.local-rag" \
  --env LOCAL_RAG_MCP_PROFILE=reader \
  -- "$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server"
codex mcp get local-rag-mcp
```

Restart or reconnect the MCP host, then call `doctor`, `sources`, and `search`. A saved registration
alone is not proof that the server can initialize. MCP profiles are capability boundaries, not
authentication: reserve `reviewer` and `admin` for trusted local processes only.

The generic stdio configuration is also available in
[config/mcp.json.example](config/mcp.json.example). Agent behavior is documented in
[docs/agents.md](docs/agents.md).

## Data, privacy, and operational limits

- Source files and Drive content remain authoritative. The data root contains derived text and may
  contain sensitive content, so protect and back it up as described in the deployment guide.
- Local OCR and local embeddings stay on the machine after their optional runtime/model downloads.
  Remote embedding providers are never enabled implicitly and receive indexed chunks and queries.
- Google Drive uses an operator-created read-only OAuth token. Keep token files and client secrets
  outside the repository; never paste their contents into an agent conversation.
- Continuous indexing is optional. Manual CLI/MCP indexing works without any background service.
- SQLite plus in-process vector scoring targets personal or team-sized corpora, not millions of
  chunks. Windows is not a supported release target yet.

Read [SECURITY.md](SECURITY.md) before exposing the service to agents or configuring remote
providers.

## Documentation

- [Vietnamese overview](README.vi.md)
- [Operator setup](docs/setup.md)
- [Deployment, upgrade, rollback, and uninstall](docs/deployment.md)
- [Agent workflow](docs/agents.md)
- [Feature and command reference](docs/reference.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
