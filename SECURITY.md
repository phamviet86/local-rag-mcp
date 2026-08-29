# Security policy

## Reporting a vulnerability

After this repository is published on GitHub, use its private **Report a vulnerability** workflow
when available. If private vulnerability reporting is unavailable, contact the repository
maintainer privately before disclosing details. Do not open a public issue containing credentials,
private document text, source paths, database contents, or an unpatched exploit.

Include the affected version or commit, operating system, configuration with secrets removed,
reproduction steps, and expected impact. There is currently no guaranteed response or disclosure
timeline for this pre-1.0 project.

## Supported versions

Security fixes are applied to the latest commit on the main branch. Older pre-1.0 snapshots are not
maintained as separate supported release lines.

## Trust and data boundaries

- Source documents remain authoritative, but SQLite and artifact/revision directories contain
  derived copies of their content. Protect the configured data root accordingly.
- Reader, reviewer, and admin MCP modes do not provide authentication or sandbox untrusted agents.
  Expose mutation modes only to trusted local processes.
- Local OCR and local embedding inference stay on the machine, apart from downloading pinned model
  and runtime assets from the documented upstream sources.
- OpenAI-compatible embeddings send document chunks and search queries to the configured remote
  endpoint. Credentials are read from environment variables and must not be committed.
- Runtime downloads are pinned and checksum verified. Treat changes to their URLs, versions, or
  checksums as security-sensitive review items.
