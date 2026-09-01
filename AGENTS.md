# Agent operating contract

`local-rag-mcp` is a standalone local service. Agents integrate through its CLI or MCP server;
internal Python modules are not a supported agent API. The package distribution is
`phamviet-local-rag-mcp`; never direct users to install the unrelated PyPI project named
`local-rag-mcp`. The project is Apache-2.0 and v0.7.0 is distributed through this repository's
GitHub Release; it does not currently publish packages to PyPI.

Start with `doctor`, `status`, and `sources`. Search without scope is global across enabled sources;
when a source or folder is supplied, treat it as strict. Cite returned source identity, path, URL,
hash/revision, page, and locator. Use reader profile by default, reviewer only for evidenced
corrections/metadata, and admin only for operator-approved indexing or source changes.

If no source exists, ask the operator to choose a local folder or a Google Drive root/account.
`no_enabled_sources` is an expected initialized state, not a service failure. Never ask for or echo
OAuth tokens, API keys, or client-secret contents. Missing OCR and embeddings are supported degraded
modes: OCR-routed PDF pages enter review, while FTS remains available without vectors. Source removal
purges only derived local state and must never touch authoritative files.

Index mutations use the durable job queue. Reader progress may expose source names and aggregate
counts but must not expose target filenames or detailed file errors. The optional OS user service is
operator-managed and must never be installed implicitly by setup or an agent. See
[docs/agents.md](docs/agents.md) for the retrieval and capability workflow.

For a coding checkout, use the isolated development bootstrap and change-validation sequence in
[CONTRIBUTING.md](CONTRIBUTING.md). Never commit secrets, publish, create a release, or push without
explicit authority.
