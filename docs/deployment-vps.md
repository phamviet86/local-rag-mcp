# Deployment on a VPS over SSH

This guide runs the index and the `serve` daemon on a VPS, while the MCP server for each client is
started on demand through SSH. There is no network listener: the client launches
`ssh -T vps env ... local-rag-mcp-server` and speaks MCP over that ssh session's stdin/stdout.
Authentication is SSH. Profiles are capability boundaries, not authentication.

claude.ai on the web and in the mobile app is **not supported yet**: it needs a remote HTTP MCP
endpoint with OAuth, which this release does not provide (planned for later). Use Claude Code,
Claude Desktop or Codex on a machine that can reach the VPS over SSH.

## 1. Install the release wheel on the VPS

Install the GitHub Release wheel into a dedicated virtual environment, exactly as in the
[README](../README.md#manual-installation-and-optional-integrations). Never run
`pip install local-rag-mcp`; that PyPI project is unrelated to this software.

```bash
mkdir -p "$HOME/.local/share/local-rag-mcp"
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install \
  "https://github.com/phamviet86/local-rag-mcp/releases/download/v0.11.0/phamviet_local_rag_mcp-0.11.0-py3-none-any.whl"
# Optional extras, same wheel: "phamviet-local-rag-mcp[google-drive] @ <wheel URL>"
```

For checksum verification see [deployment](deployment.md#verify-and-install-a-release-wheel). The
executables are then `$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp` and
`.../local-rag-mcp-server`; clients need their absolute paths.

## 2. Set up the data root and sources

```bash
DATA_ROOT="$HOME/.local-rag"
local-rag-mcp --home "$DATA_ROOT" setup --no-ocr
local-rag-mcp --home "$DATA_ROOT" source add-local notes /srv/documents
# or a Google Drive source, see drive-sync.md and setup.md
local-rag-mcp --home "$DATA_ROOT" reconcile --source notes
```

Use `setup --full` instead of `--no-ocr` to provision local OCR on the VPS (scanned PDFs). OCR
runs on the VPS only; clients need nothing extra.

### Embedding configuration (VPS only)

Embedding API keys belong in the VPS, in `DATA_ROOT/credentials/embeddings.json` with mode `0600`
inside a `0700` directory (see [setup](setup.md#persistent-embedding-settings)). Do not put a key in
the client configuration, in the ssh command line, or in any `env` argument: command lines are
visible in process lists and logs. The server reads the file itself, so clients never see it.

```bash
install -d -m 0700 "$DATA_ROOT/credentials"
umask 077 && "${EDITOR:-vi}" "$DATA_ROOT/credentials/embeddings.json"
chmod 0600 "$DATA_ROOT/credentials/embeddings.json"
```

## 3. Run the `serve` daemon under systemd

The daemon executes queued index jobs and the per-source sync schedule. The unit below is an example
only: **the operator installs this manually**. Neither `setup` nor an agent installs it.

```ini
# ~/.config/systemd/user/local-rag-mcp.service   (operator installs this manually)
[Unit]
Description=local-rag-mcp serve daemon
After=network-online.target

[Service]
Type=simple
Environment=LOCAL_RAG_MCP_HOME=%h/.local-rag
ExecStart=%h/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now local-rag-mcp.service
loginctl enable-linger "$USER"   # keep the user service running after logout
```

The daemon and the SSH-launched MCP processes share the same SQLite data root and the same durable
job queue.

## 4. SSH client configuration

Each MCP session is an ssh connection. Reuse one authenticated connection so start-up stays fast and
no password prompt can interrupt MCP. Put this in `~/.ssh/config` on the client and load the key into
ssh-agent or the OS keychain (`ssh-add --apple-use-keychain ~/.ssh/id_ed25519` on macOS):

```sshconfig
Host vps
    HostName vps.example.com
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    AddKeysToAgent yes
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
    ServerAliveInterval 30
```

Use `-T` (no TTY) so the stdio stream stays clean. Never run an interactive shell profile that
prints banners to stdout.

## 5. Client configuration

Replace the two absolute paths with the real ones on the VPS. The `admin` profile is shown because
the operator drives indexing remotely; use `reader` for ordinary retrieval clients.

### Claude Code

```bash
claude mcp add local-rag-vps -- ssh -T vps env \
  LOCAL_RAG_MCP_HOME=/home/ubuntu/.local-rag \
  LOCAL_RAG_MCP_PROFILE=admin \
  /home/ubuntu/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "local-rag-vps": {
      "command": "ssh",
      "args": [
        "-T", "vps", "env",
        "LOCAL_RAG_MCP_HOME=/home/ubuntu/.local-rag",
        "LOCAL_RAG_MCP_PROFILE=admin",
        "/home/ubuntu/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server"
      ]
    }
  }
}
```

### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.local-rag-vps]
command = "ssh"
args = [
  "-T", "vps", "env",
  "LOCAL_RAG_MCP_HOME=/home/ubuntu/.local-rag",
  "LOCAL_RAG_MCP_PROFILE=admin",
  "/home/ubuntu/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server",
]
```

## 6. Verify

Run `doctor` through SSH, from the client machine:

```bash
ssh -T vps /home/ubuntu/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp \
  --home /home/ubuntu/.local-rag doctor --json
```

The `daemon` check must have `"status": "ok"`. A `warning` means no `serve` daemon is running, so
scheduled sync and queued jobs will not run. Then reconnect the MCP client and call `doctor`,
`sources` and `search`. The reader tools `read` (default `length` 12000, at most 40000 characters per
call; continue with `next_start`) and `list_documents` are sized for the clients' tool-result limits.

## Limits

- claude.ai web and mobile are unsupported; HTTP transport and OAuth are future work.
- One ssh session starts one MCP process; a dropped connection ends it and the client reconnects.
- Keep the VPS data root, `credentials/` and token files private to the service user.
