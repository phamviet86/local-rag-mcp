# Changelog

All notable changes to this project are documented in this file. This project follows
[Semantic Versioning](https://semver.org/) while it remains pre-1.0.

## [Unreleased]

## [0.8.0] - 2026-09-05

- Support arbitrary Google Drive titles without using names as local storage paths; retain stable
  file-ID hash cache keys and original titles, IDs, source URLs and folder relationships.
- Refresh rename/move metadata even when content is unchanged, and add explicit Drive ID scopes.
- Continue after per-file failures, retain retryable cursors, preserve valid partial-listing results,
  and suppress inferred deletions until a complete authoritative scan succeeds.
- Expose persistent, scoped search coverage for unindexed/stale/pending files, plus a paginated
  reader `index_coverage` MCP tool and actionable retry guidance.
- Validate the Drive fix in an isolated same-VPS trial: 142 documents, 2,376 chunks, all four
  formerly blocking slash-title Docs readable, stable full/incremental resync, and MCP retrieval.
  The trial disabled OCR/embeddings; 28 PDFs lacked native text and still require OCR/review.
- Publish fresh wheel/sdist/checksums through a v0.8.0-only job after successful main-branch CI.
  Upgrade guidance requires one full Drive reconcile; no production install or PyPI publication.

## [0.7.1] - 2026-09-01

- Add copy-ready macOS/Linux checksum verification, Python/SQLite preflight, doctor-led
  troubleshooting, and data-root-safe backup and restore guidance.
- Document Google Drive OAuth prerequisites and add an isolated contributor/coding-agent bootstrap.

## [0.7.0] - 2026-09-01

- Adopt the unique Python distribution name `phamviet-local-rag-mcp`; the repository, product, and
  CLI remain `local-rag-mcp`.
- Add release-oriented documentation for isolated wheel installs, Codex MCP registration, deployment
  backups, upgrades, rollback, and removal on macOS and Linux.
- Add an Apache-2.0 license and release metadata.
- Strengthen release verification for built distributions and clean-environment installation.

[0.8.0]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.8.0
[0.7.1]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.1
[0.7.0]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.0
