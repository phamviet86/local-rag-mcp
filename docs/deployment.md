# Deployment and lifecycle

This guide is the release path for macOS and Linux machines that should use `local-rag-mcp` normally.
It assumes a trusted local user and a released wheel. The repository is currently preparing v0.7.0;
until a maintainer creates a GitHub Release, no wheel or checksum is available to download.

## Distribution identity

| Item | Value |
| --- | --- |
| Product, repository, CLI, server executable | `local-rag-mcp` / `local-rag-mcp-server` |
| Python distribution | `phamviet-local-rag-mcp` |
| License | Apache-2.0 |

The PyPI name `local-rag-mcp` is an unrelated project. Do not install it, with or without extras.
Only install a wheel attached to this repository's reviewed GitHub Release, or—after an explicit PyPI
publication—the unique `phamviet-local-rag-mcp` distribution.

## Support matrix

| Area | Supported release target | Notes |
| --- | --- | --- |
| macOS | Yes, Python 3.11–3.13 | Optional continuous indexing uses a per-user LaunchAgent. |
| Linux | Yes, Python 3.11–3.13 | Optional continuous indexing uses a per-user systemd service. |
| Windows | Not yet supported | Do not treat it as a normal deployment target. |
| SQLite | FTS5 required | Verify with `local-rag-mcp doctor --json`. |
| OCR | Optional | `setup --full` requires a provisioning download; `--no-ocr` is supported. |
| Embeddings | Optional | Local model or remote endpoint is explicit; FTS works without either. |
| Google Drive | Optional | Requires operator-created read-only OAuth credentials and `google-drive` when packaged separately. |

## Install a release wheel

Before installing, confirm the GitHub Release belongs to `phamviet86/local-rag-mcp`, select its
attached wheel, and compare its SHA-256 checksum with the value in the release notes. Store the
download outside the repository.

```bash
mkdir -p "$HOME/.local/share/local-rag-mcp"
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install --upgrade pip
shasum -a 256 "$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
python -m pip install "$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
local-rag-mcp --help
```

The filename and checksum above are examples, not a claim that v0.7.0 has been released. Use the
exact published filename and checksum. Install wheel extras directly from that wheel:

```bash
python -m pip install "phamviet-local-rag-mcp[local-embeddings] @ file://$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
python -m pip install "phamviet-local-rag-mcp[google-drive] @ file://$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
```

If PyPI is later announced as a release channel, use only:

```bash
python -m pip install 'phamviet-local-rag-mcp[local-embeddings]'
python -m pip install 'phamviet-local-rag-mcp[google-drive]'
```

Extras may be combined if the published release supports both. Do not substitute
`local-rag-mcp[...]` in any command.

## Initialize and connect Codex

Initialize a separate data root before starting the MCP server. The default is `~/.local-rag`; use a
different absolute path when home directories are ephemeral or when data must live on an encrypted
volume.

```bash
local-rag-mcp setup --no-ocr
local-rag-mcp doctor --json

codex mcp add local-rag-mcp \
  --env LOCAL_RAG_MCP_HOME="$HOME/.local-rag" \
  --env LOCAL_RAG_MCP_PROFILE=reader \
  -- "$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server"
codex mcp get local-rag-mcp
```

Restart/reconnect the host and make an MCP `doctor`, `sources`, and `search` call. With no sources,
`no_enabled_sources` is the expected search response. Add a source only after the owner identifies
it; see [setup.md](setup.md).

## Backup and migrate state

Stop optional indexing before copying state so the snapshot is consistent:

```bash
local-rag-mcp service stop
mkdir -p "$HOME/local-rag-backups"
tar -C "$HOME" -czf "$HOME/local-rag-backups/local-rag-$(date +%Y%m%d).tgz" .local-rag
local-rag-mcp service start
```

If the optional service was never installed, omit the first and last commands. Protect backups like
the live data root: they can contain extracted document text, OCR corrections, Drive cache bytes,
and token-path metadata. Do not put them in the repository or a public artifact store.

To move the state to another machine, copy the archive through an approved protected channel, extract
it under the target user's home (or selected `LOCAL_RAG_MCP_HOME`), then run:

```bash
local-rag-mcp doctor --json
local-rag-mcp source list
```

Local source paths must exist and be readable on the destination. Drive token files may need to be
provisioned again according to organizational policy; do not blindly transfer credentials. Schema
migrations are applied when the CLI/server opens the database. Keep the pre-upgrade backup until you
have verified search and reads on the destination.

## Upgrade and rollback

1. Record the installed version: `python -m pip show phamviet-local-rag-mcp`.
2. Stop the optional service and make a backup.
3. Download and checksum-verify the new release wheel.
4. Install the new wheel into the same isolated environment.
5. Run `local-rag-mcp doctor --json`, `source list`, and a representative `search`/`read` check.
6. Restart the optional service only after those checks pass.

```bash
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
local-rag-mcp service stop
python -m pip install --upgrade "$HOME/Downloads/phamviet_local_rag_mcp-NEW_VERSION-py3-none-any.whl"
local-rag-mcp doctor --json
local-rag-mcp service start
```

For a failed upgrade, stop the service, reinstall the previously verified wheel, and restore the
pre-upgrade archive if the newer program migrated the state incompatibly. Then verify doctor and a
representative read before restart. Never delete a data root as part of ordinary rollback.

## Uninstall

Unregister MCP and remove the optional service first. Removing the virtual environment does not
remove source files; deleting the data root is a separate, irreversible choice.

```bash
codex mcp remove local-rag-mcp
local-rag-mcp service uninstall
```

After confirming the backup and no longer needing local derived data, remove only the dedicated
environment manually. Preserve `~/.local-rag` unless the operator explicitly wants to discard its
index, cached derived content, OCR reviews, and configuration. Source directories and Google Drive
items are never deleted by this project.

## Release operator checklist

Before publishing, confirm the tag/version, Apache-2.0 license, clean build of wheel and sdist,
distribution metadata, checksum, isolated wheel installation, `setup --no-ocr`, SQLite/FTS5,
MCP initialize/tool list, and existing test/CI gates. Attach wheel, sdist, checksums, and concise
release notes to the GitHub Release. Publish to PyPI only under `phamviet-local-rag-mcp` after the
name is rechecked.
