# Security policy

## Release status

The public [v0.8.0 GitHub Release](https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.8.0)
was published on 2026-09-05. Its Python distribution is `phamviet-local-rag-mcp`; this project does
not currently publish to PyPI. The existing PyPI project named `local-rag-mcp` is unrelated. Do not
install, report vulnerabilities to, or infer support status from that other project.

The code is licensed under Apache-2.0. Security fixes target the latest supported release or the
latest commit on `main` while the project remains pre-1.0. Older snapshots are not maintained as
separate security lines.

## Reporting a vulnerability

Use GitHub's private **Report a vulnerability** workflow when it is enabled for this repository.
Otherwise contact the maintainer privately before disclosure. Do not open a public issue containing
credentials, private document text, source paths, database contents, or an unpatched exploit.

Include the affected version or commit, operating system, redacted configuration, reproduction steps,
and expected impact. There is no guaranteed response or disclosure timeline for this pre-1.0 project.

## Trust and data boundaries

- Source documents remain authoritative, but SQLite and artifact/revision directories contain
  derived copies of their content. Protect the configured data root and backups accordingly.
- Reader, reviewer, and admin MCP profiles do not authenticate or sandbox callers. Expose mutation
  profiles only to trusted local processes.
- The supported agent boundary is the CLI or MCP server. Internal Python service classes are not a
  stable public agent API and should not be imported into untrusted agent runtimes.
- Local OCR and local embedding inference stay on the machine after optional runtime/model downloads.
- OpenAI-compatible embeddings send document chunks and search queries to the configured remote
  endpoint. Credentials are read from environment variables and must not be committed.
- Google Drive adapters request the read-only Drive scope. OAuth token files must remain outside the
  repository with owner-only permissions; the source registry stores only their paths.
- Runtime downloads are pinned and checksum verified. Treat changes to their URLs, versions, or
  checksums as security-sensitive review items.

## Release integrity

Install only wheel/sdist artifacts attached to this repository's official GitHub Release and verify
the published checksum before use. A release should be built from a clean tag and pass the packaging,
isolated-install, CLI/MCP smoke, and test/CI checks documented in [docs/deployment.md](docs/deployment.md).
