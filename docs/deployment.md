# Deployment and lifecycle

This guide is the release path for macOS and Linux machines that should use `local-rag-mcp` normally.
It assumes a trusted local user and a released wheel. **v0.7.1** was released on 2026-09-01; use the
[v0.7.1 GitHub Release](https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.1) for its
wheel, source archive, and SHA-256 checksums.

## Distribution identity

| Item | Value |
| --- | --- |
| Product, repository, CLI, server executable | `local-rag-mcp` / `local-rag-mcp-server` |
| Python distribution | `phamviet-local-rag-mcp` |
| License | Apache-2.0 |

The PyPI name `local-rag-mcp` is an unrelated project. Do not install it, with or without extras.
Only install the unique `phamviet-local-rag-mcp` distribution from this repository's reviewed GitHub
Release. This project does not currently publish packages to PyPI.

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

## Preflight

Run these checks before creating the virtual environment. They do not create project state. The
SQLite check must print `FTS5 available`; if it fails, install a supported Python 3.11–3.13 build
whose bundled SQLite has FTS5, then repeat the check. This project does not maintain operating-system
package-manager instructions.

```bash
python3.11 --version
python3.11 -m venv --help >/dev/null
python3.11 - <<'PY'
import sqlite3

connection = sqlite3.connect(":memory:")
connection.execute("CREATE VIRTUAL TABLE probe USING fts5(value)")
print("FTS5 available")
PY
```

## Verify and install a release wheel

Create a dedicated production environment first; cloning the repository is not required. Then choose
either the convenient direct URL install or the recommended verified local-file install. For the
latter, download the wheel, compare its SHA-256 digest with `SHA256SUMS` on the
[release page](https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.1), then install the
verified local file.

```bash
mkdir -p "$HOME/.local/share/local-rag-mcp"
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install --upgrade pip
```

### Direct URL install

```bash
python -m pip install \
  "https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
local-rag-mcp --help
```

To verify the exact v0.7.1 wheel on a supported platform, download only the named wheel and the
checksum manifest, then validate the wheel entry. `shasum` is used on macOS and `sha256sum` on Linux;
both commands must print `OK` before installation.

```bash
RELEASE_URL="https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1"
WHEEL="phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
mkdir -p "$HOME/Downloads/local-rag-mcp-v0.7.1"
cd "$HOME/Downloads/local-rag-mcp-v0.7.1"
curl -fL -O "$RELEASE_URL/$WHEEL"
curl -fL -O "$RELEASE_URL/SHA256SUMS"
grep -F "  $WHEEL" SHA256SUMS > "$WHEEL.sha256"
test -s "$WHEEL.sha256"

case "$(uname -s)" in
  Darwin) shasum -a 256 -c "$WHEEL.sha256" ;;
  Linux)  sha256sum -c "$WHEEL.sha256" ;;
  *) echo "Unsupported release target; see the support matrix."; exit 1 ;;
esac
rm -f "$WHEEL.sha256"

. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install "$WHEEL"
local-rag-mcp --version
```

Use `SHA256SUMS` attached to the v0.7.1 release as the source of truth; do not hard-code a digest in
local deployment notes. Do not use `pip install local-rag-mcp` or a checksum copied from an untrusted
issue, chat, or mirror.

Install wheel extras directly from the same release wheel:

```bash
python -m pip install \
  "phamviet-local-rag-mcp[local-embeddings] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
python -m pip install \
  "phamviet-local-rag-mcp[google-drive] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
```

Extras may be combined if needed. Do not substitute `local-rag-mcp[...]` in any command: that PyPI
package is unrelated to this project.

## Initialize and connect Codex

Initialize a separate data root before starting the MCP server. The default is `~/.local-rag`; use a
different absolute path when home directories are ephemeral or when data must live on an encrypted
volume. Set one `DATA_ROOT` value and use it consistently for CLI, optional service, and MCP
registration. The `--home` option belongs before the subcommand.

```bash
DATA_ROOT="$HOME/.local-rag"
local-rag-mcp --home "$DATA_ROOT" setup --no-ocr
local-rag-mcp --home "$DATA_ROOT" doctor --json

codex mcp add local-rag-mcp \
  --env LOCAL_RAG_MCP_HOME="$DATA_ROOT" \
  --env LOCAL_RAG_MCP_PROFILE=reader \
  -- "$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server"
codex mcp get local-rag-mcp
```

Restart/reconnect the host and make an MCP `doctor`, `sources`, and `search` call. With no sources,
`no_enabled_sources` is the expected search response. Add a source only after the owner identifies
it; see [setup.md](setup.md).

## Troubleshooting from `doctor`

Start every diagnosis with `local-rag-mcp --home "$DATA_ROOT" doctor --json`. Exit code `2` signals
a blocked readiness condition or command error; `doctor` can report a non-blocking `degraded` state
with exit code `0`. Use its structured `checks` and `actions` instead of guessing. An initialized
zero-source state is expected: it returns
`no_enabled_sources` until an operator deliberately adds and reconciles a source.

| Symptom | Safe next action |
| --- | --- |
| `python3.11` or `venv` is missing | Install a supported Python 3.11–3.13 build using the platform's maintained channel, then rerun [Preflight](#preflight). Do not substitute an unsupported interpreter. |
| The FTS5 preflight or `doctor` database check fails | Use a supported Python build with SQLite FTS5, restore a known-good data-root backup if integrity failed, then run `local-rag-mcp --home "$DATA_ROOT" doctor --json`. Do not delete the data root as a first response. |
| Checksum command does not print `OK` | Delete the downloaded wheel and manifest, fetch both again from the official release, and stop if the digest still differs. Do not install that artifact. |
| MCP cannot initialize or sees the wrong index | Confirm the MCP registration's absolute executable and `LOCAL_RAG_MCP_HOME` equal the CLI's environment and `DATA_ROOT`; run `codex mcp get local-rag-mcp`, reconnect the host, then call MCP `doctor`, `sources`, and `search`. |
| Optional service cannot start or misses updates | Run `local-rag-mcp --home "$DATA_ROOT" service status`. Inspect the platform's per-user service logs (LaunchAgent on macOS; `journalctl --user` for the systemd unit on Linux), then use manual reconcile while investigating. The service is optional. |
| Drive authentication/sync fails | Recheck that the Drive API, consent screen, and Desktop OAuth client were created by the operator; rerun `auth-google` locally and inspect `doctor`/sync errors without revealing token or client-secret contents. |
| OCR or embeddings are unavailable | `--no-ocr` and full-text search are supported degraded modes. For OCR, rerun `setup --full` only with an approved network. For embeddings, confirm the selected optional package/provider and rerun `reindex --all`; never paste provider keys into chat or the repository. |

## Backup and migrate state

Stop optional indexing before copying state so the snapshot is consistent:

```bash
DATA_ROOT="/absolute/path/to/.local-rag"
ARCHIVE="$HOME/local-rag-backups/local-rag-$(date +%Y%m%d).tgz"
local-rag-mcp --home "$DATA_ROOT" service stop
mkdir -p "$HOME/local-rag-backups"
tar -C "$DATA_ROOT" -czf "$ARCHIVE" .
local-rag-mcp --home "$DATA_ROOT" service start
```

If the optional service was never installed, omit the first and last commands. Protect backups like
the live data root: they can contain extracted document text, OCR corrections, Drive cache bytes,
and token-path metadata. Do not put them in the repository or a public artifact store.

To move the state to another machine, copy the archive through an approved protected channel, create
the target root, and extract into that same root. Use the target's `DATA_ROOT` for every subsequent
CLI, service, and MCP command:

```bash
DATA_ROOT="/absolute/path/to/.local-rag"
ARCHIVE="/approved/path/local-rag-YYYYMMDD.tgz"
mkdir -p "$DATA_ROOT"
tar -C "$DATA_ROOT" -xzf "$ARCHIVE"
local-rag-mcp --home "$DATA_ROOT" doctor --json
local-rag-mcp --home "$DATA_ROOT" source list
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
5. Run `local-rag-mcp --home "$DATA_ROOT" doctor --json`, `source list`, and a representative `search`/`read` check.
6. Restart the optional service only after those checks pass.

```bash
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
DATA_ROOT="/absolute/path/to/.local-rag"
local-rag-mcp --home "$DATA_ROOT" service stop
python -m pip install --upgrade "$HOME/Downloads/phamviet_local_rag_mcp-NEW_VERSION-py3-none-any.whl"
local-rag-mcp --home "$DATA_ROOT" doctor --json
local-rag-mcp --home "$DATA_ROOT" service start
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

## Maintainer checklist for future releases

Before publishing a later release, confirm the tag/version, Apache-2.0 license, clean build of wheel and sdist,
distribution metadata, checksum, isolated wheel installation, `setup --no-ocr`, SQLite/FTS5,
MCP initialize/tool list, and existing test/CI gates. Attach wheel, sdist, checksums, and concise
release notes to the GitHub Release. Publish to PyPI only under `phamviet-local-rag-mcp` after the
name is rechecked.
