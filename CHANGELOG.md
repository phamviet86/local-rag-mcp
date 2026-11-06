# Changelog

All notable changes to this project are documented in this file. This project follows
[Semantic Versioning](https://semver.org/) while it remains pre-1.0.

## [Unreleased]

## [0.11.0] - 2026-11-07

See [v0.11.0 release notes](docs/releases/v0.11.0.md).

- Merge the four per-version release workflows into `.github/workflows/release.yml`: it derives the
  version from `pyproject.toml`, requires `docs/releases/v<version>.md` and the `[release
  v<version>]` commit marker, fails when the tag already exists on another commit, and keeps
  the existing verified-draft publish steps.
- The `local-rag` alias now prints a deprecation warning to stderr and will be removed in the next
  minor release; use `local-rag-mcp`.
- Add the `legal_chunking` setting (default `true`). When `false`, the Điều/khoản splitter is skipped:
  Markdown splits by heading and other files use window chunks with the context prefix. Run `reindex`
  (optionally `--reextract`) after changing it; chunks are rebuilt from the cached extraction.
- Docs: for a single user over SSH use `LOCAL_RAG_MCP_PROFILE=admin`; profiles are tool scope, not
  authentication. Mark implemented items in the agent RAG service design.

- Index HTML/HTM, CSV, JSON, EPUB and ODT/ODS/ODP files (standard library only) locally and from
  Drive (new MIME types). Locators: `block:N`, `row:N`, JSON paths, `chapter:N`,
  `paragraph:N`/`sheet:S/row:N`/`slide:N`. A saved extension list equal to the old default adopts
  the new defaults. Add the optional `markitdown` extra and `markitdown_extensions` (default `[]`)
  to convert listed legacy extensions; a missing library fails only those files
  (`code=markitdown_missing`).
- `get_page_image` and `check_status` now work for Drive PDFs through the raw download cache
  (`cache_matches_index`, `integrity.checked: true`); without the cache the error is
  `page_image_unavailable` (run `refresh`).
- PDF `complex_layout` is now a document flag (`layout_flags`, `layout_pages`, returned by `search`
  and `read`) instead of a review item, so `doctor` is no longer `warning` because of it. Migration
  13 resolves open `complex_layout` reviews (`migrated_to_layout_flag`) and writes the flag to their
  documents; OCR reviews are unchanged.
- `eval` now also reports `mrr@10` and `ndcg@10`, overall and per group; the full-text baseline is
  recorded in [docs/eval-baseline.md](docs/eval-baseline.md).
- Add an optional cross-encoder reranker: settings `rerank_provider` (`none` default | `local`),
  `rerank_model` (default `BAAI/bge-reranker-v2-m3`) and `rerank_top_n` (default 30, env
  `LOCAL_RAG_MCP_RERANK_PROVIDER/MODEL/TOP_N`), install with the `rerank` extra. It reorders the top
  candidates by the full chunk text after hybrid scoring and before document-number priority;
  reordered hits carry `reranked: true` and `rerank_score`. A failure adds a search warning and keeps
  the original order. `status` and `doctor` report the reranker (provider, model, available).
- Window (non-structured) chunks now start with a context line `title | relative_path`, the same
  `a | b` format as structured chunks, so FTS and embeddings see the document context.
  `start_char`/`end_char` still point at the body, so a hit's text is the context line plus the
  source slice. Window chunks' `chunk_hash` now covers the stored text including the context line,
  as structured chunks already did, so vectors are keyed per context. Migration 12 adds
  `chunks.body_hash` (backfilled from `chunk_hash`; hash of the body alone for window chunks), which
  duplicate collapse, search `locations` and the `DUPLICATE_CONTENT` check use, so identical copies
  still collapse in results. Existing data needs `reindex --reextract` to apply this: window chunks
  get new hashes and are re-embedded, and their old vectors become orphans that the next orphan
  purge (artifact cleanup or source removal) deletes. The path in a context line is not rewritten
  when a file is moved until the next reextract.
- With a reranker configured, search gathers `max(limit, rerank_top_n)` candidates, reranks, then cuts
  to `limit`. `eval` nDCG@10 is binary DCG over all gold keys (each credited once).
- Add the reader MCP tool and CLI `list [--source --folder --media-type --doc-type --modified-after
  --modified-before --include-trashed --limit --cursor]` (`list_documents`): keyset-paginated
  listing ordered by `(source_id, id)` with an opaque `next_cursor` and `total`; trashed Drive
  files are hidden unless `include_trashed`. An invalid cursor is an error.
- `read` now caps `length` and `window` at 40000 characters (was 100000) to fit client tool-result
  limits, and returns `next_start` (the `end` offset, or `null` at the end of the text).
- Add [docs/deployment-vps.md](docs/deployment-vps.md): VPS install, `serve` under systemd (operator
  installs manually) and MCP over `ssh -T` for Claude Code, Claude Desktop and Codex. claude.ai
  web/mobile is not supported yet.
- Drive trash is distinguished from permanent deletion (migration 11, `documents.trashed_at`). A
  trashed file, or any file below a trashed folder, keeps its chunks, vectors and raw cache but is
  hidden from `search`, `get_chunk` and `index_coverage`; `read`, `metadata`, `check_status` and
  `page_image` return `document_trashed` (`read --include-trashed` / `include_trashed` still reads
  it). Restoring it makes it searchable again without a download unless the checksum changed. The
  sync report gains `trashed` and `restored`. A scan error never trashes or deletes anything.
- Hard deletes now clean up: the Drive raw export, the extraction artifact (unless another document
  shares it) and orphaned vectors are removed, for Drive and for local files. Every full Drive
  reconcile also removes unreferenced raw files and artifacts older than an hour, reporting
  `gc_removed`.
- Google Drive sources can use a service account: `source add-drive ... --service-account-key PATH`
  (mutually exclusive with `--token-file`; key file must be `0600` and is hidden from `sources`).
- Google Docs are now exported as Markdown (`.md`) so headings become `section_path`. Existing Docs
  indexed as `.txt` are re-exported and reindexed once at the next full scan.
- Per-source sync schedule: `source set NAME --poll-seconds N --full-reconcile-seconds N` stores
  `poll_seconds` (Drive 300, local 600, minimum 60) and `full_reconcile_seconds` (43200) in the
  source config. The `serve` daemon replaces its global `reconcile_seconds` timer with a scheduler
  that enqueues `reconcile(source)` (and a Drive `full` reconcile, tracked in
  `last_full_reconcile_at`) only when no job is active.
- Add the reader MCP tool and CLI `refresh [--source NAME]`: it only enqueues an incremental
  reconcile, at most once per 60 seconds per source (`rate_limited` with `retry_after_seconds`),
  and returns the job with its `executor`.
- Add data freshness: `sources`, `status` and `search.coverage.freshness` report `last_synced_at`,
  `last_attempt_at`, `next_sync_at` and `poll_seconds` (`last_synced_at` is the last successful
  sync, stored in `source_sync_state` key `last_success_at`, no migration); `doctor` warns `stale`
  when a source has had no successful sync for 3 x `poll_seconds`.
- The `serve` daemon is now the job executor: it publishes a heartbeat (migration 10,
  `daemon_heartbeat`, every 10 seconds) and runs `queued` index jobs left by other processes. When a
  daemon is alive, MCP/CLI `start_index_job` callers only enqueue and report `"executor": "daemon"`
  (`"this_process"` otherwise). `doctor` gains a non-blocking `daemon` check.
- Fix the job queue: `queued` jobs older than 20 minutes now expire to `error`, CLI
  `reconcile`/`reindex` no longer run a job that is rejected or already running, and watcher file
  events rejected by an active job are retried instead of lost.
- Add structure-aware chunking for Vietnamese legal text: one chunk per Điều (split by khoản when
  longer than `structure_max_chars`, default 2000), a context line `số hiệu | tên | Chương | Điều`,
  Markdown heading sections, and a fixed-window fallback (`chunk_strategy`: `structure` default or
  `window`). Document numbers such as `66.13/2026/NQ-CP` are detected on the first page.
- Migration 8 adds `chunks.cite_key/section_path/page_start/page_end/ocr` and
  `documents.doc_no/doc_no_key`. Existing rows stay NULL until `reindex --reextract`; because chunk
  text changes, chunk hashes change and embeddings are recomputed.
- `search` gains `match` (`auto` default, `any`, `all`, `phrase`) and `doc_no`; results add `cite_key`,
  `page_start`, `page_end`, `ocr`, `ocr_notice`, `section_path`, `doc_no`. A query that is a
  document number looks the document up first.
- Add read-only MCP tool and CLI `get_chunk` / `chunk <cite_key> --neighbors N`, and
  `eval <file.jsonl> --k --target` reporting recall@1/3/10 with failure groups (exit 1 below target).
- Split long khoản into điểm chunks (`...-khoan-2-diem-a`) and add the document title to the chunk
  context line.
- Migration 9 adds `document_validity` (`effective_from`, exclusive `effective_to`, `amended_by`,
  `replaced_by`, `status_checked_on`, `status_source`). Unknown validity is never treated as valid.
  Reviewer `set_validity` / CLI `validity set` record it with evidence; reader `check_status` /
  CLI `check-status --on DATE` report status, `CITES_EXPIRED`, `DUPLICATE_SHA256`,
  `DUPLICATE_CONTENT`, `SOURCE_CHANGED` (local file re-hash), OCR and open-review warnings.
- `search` gains `doc_type` (document code such as NĐ-CP, TT), `valid_on` (YYYY-MM-DD, keeps only
  documents with recorded validity covering the date) and `match=auto` (AND, then fall back to OR).
- Document Claude Code and Claude Desktop MCP setup.
- Raise `pdf-inspector` from 1.15.0 to 1.25.2 (no API changes needed for the calls this project
  uses; the upstream changelog lists no page-rendering API). Re-run `reindex --reextract` to pick
  up its extraction fixes.
- Add reader MCP tool `get_page_image` / CLI `page-image --page N --out FILE`: renders one page of
  an indexed local PDF to PNG through the PDFium library installed by `setup --full` (ctypes plus
  zlib, no new Python dependency) so a citation can be checked against the original page. It
  reports whether the file still matches the indexed hash. Drive files and non-PDFs are refused.
- Legal splitter hardening from real PDFs: strips pdf-inspector Markdown (`## Điều 1.`,
  `**Điều 2.**`, inline headings after a sentence), tolerates running page headers and skipped
  article numbers, keeps a `mo-dau` chunk for the preamble, packs oversized pieces into `-ptN`
  parts, and prefers the document's own `Số:` number over cited numbers. `match=auto` (now the default after a 24-question check on nine real documents: hybrid recall@1/3/10 0.83/0.92/0.96 versus 0.79/0.83/0.96 for `any`) lists
  all-terms matches first and then partial matches.
- Verified on three real public PDFs from datafiles.chinhphu.vn (Luật Đường bộ 35/2024/QH15, a
  consolidated VBHN of Nghị định 156/2018, Thông tư 39/2025/TT-BNNMT): 86/86, 91/92 and 8/8
  Điều found, document numbers read correctly. With `qwen/qwen3-embedding-8b` through OpenRouter
  on 12 questions, hybrid recall@1/3/10 was 0.92/1.00/1.00 (full-text only 0.58/0.75/1.00).
- Measured OCR limit: on a synthetic scanned page the PP-OCRv6 small model gave CER 0.14–0.15 and
  dropped many Vietnamese vowels (`Điều` read as `Điu`) while reporting confidence 0.93–0.99.
  Numbers and amounts survived, but folded search cannot match the lost letters; treat OCR pages as
  unverified and check them with `get_page_image`. OCR pages often arrive without line breaks, so
  they fall back to fixed windows.
- Second real-data pass (nine current and five 2010–2012 Công báo PDFs, installed wheel,
  `qwen/qwen3-embedding-8b`, 24 questions): hybrid recall@1/3/10 0.79/0.92/0.96, full-text only
  0.67/0.79/0.92. Document numbers are read from the `Số:` / `Luật số:` line (spaces around
  slashes tolerated; a draft with a blank number no longer borrows a cited Nghị định's number).
  Forms after the last article (`Phụ lục`, `(Kèm theo ...)`) become `phu-luc` chunks instead of
  being filed under the last Điều, and rank slightly below articles.
- Still unverified: large-corpus performance, real scanned legal PDFs, and the one article the
  consolidated VBHN text appears to skip.

## [0.10.1] - 2026-10-19

- Repair only hash-prefixed 6/8-digit XLSX style colors in a private, bounded temporary copy;
  preserve source bytes, number formats, text and cell locators.
- Expose compatibility repair metadata and operator warnings; report content-free XLSX error codes
  with parse/download/index stages, retaining reader redaction and separate embedding warnings.
- Reject unsafe archives, stylesheet declarations and missing worksheet parts explicitly.
- Support direct upgrades from 0.9.0 and 0.10.0 without adding a schema migration.

## [0.10.0] - 2026-10-19

- Abort incomplete local discovery without deleting indexed state; detect unique content moves.
- Split Drive content/location keys with v2 compatibility and metadata-only updates.
- Add admin source relocation through the durable queue, stable refs, hit offsets and read-around.
- Rebuild Vietnamese FTS without re-embedding; diversify hits and preserve duplicate citations.
- Record DOCX headings with opt-in document order; add optional float32 matrix scoring and filters.
- Preserve populated v0.9.0 documents, vectors and OCR revisions during in-place migrations.
- Redact failure filenames/details from reader MCP coverage and expose local source health.


## [0.9.0] - 2026-09-13

- Bundle `local-rag-setup` and `local-rag` agent skills in wheel/sdist and install them with a
  deterministic `install-skills` CLI, runtime discovery, read-only checks, and managed updates.
- Add agent-first English/Vietnamese release-wheel onboarding and installation/upgrade instructions.
- Load optional protected embedding settings from the data home for persistent CLI/MCP/service
  configuration, with environment precedence and redacted validation errors.
- Preserve MCP SDK version negotiation and structured results; report unavailable retrieval as tool
  errors, unknown tools as invalid requests, and remote-capable search as open-world.
- Verify wheel-installed skills and real SDK retrieval in macOS/Linux CI; publish v0.9.0 only from
  the exact successful main CI commit carrying `[release v0.9.0]`.

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
[0.9.0]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.9.0
[0.7.1]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.1
[0.7.0]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.0

[0.10.0]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.10.0

[0.10.1]: https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.10.1
