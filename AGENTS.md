# Agent operating contract

`local-rag-mcp` is a standalone local service. Agents integrate through its CLI or MCP server;
internal Python modules are not a supported agent API.

Start with `doctor`, `status`, and `sources`. Search without scope is global across enabled sources;
when a source or folder is supplied, treat it as strict. Cite the returned source identity, path,
URL, hash/revision, page, and locator. Use reader profile by default, reviewer only for evidenced
corrections/metadata, and admin only for operator-approved indexing or source changes.

If no source exists, ask the operator to choose a local folder or a Google Drive root/account. Never
ask for or echo OAuth tokens, API keys, or client-secret contents. Missing OCR and embeddings are
supported degraded modes: OCR-routed PDF pages enter review, while FTS remains available without
vectors. Source removal purges only derived local state and must never touch authoritative files.

Run `ruff format --check .`, `ruff check .`, `mypy`, `pytest`, and `python -m compileall -q src tests`
before committing. Never commit secrets, publish, or push without explicit authority.
