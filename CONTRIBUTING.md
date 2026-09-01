# Contributing and coding-agent setup

This guide is for a source checkout. End users should install a published wheel through
[docs/deployment.md](docs/deployment.md), not use an editable install.

## Bootstrap an isolated checkout

Use Python 3.11–3.13 and keep runtime data outside the repository. Do not reuse a production virtual
environment or point test runs at a real `~/.local-rag` directory.

```bash
git clone https://github.com/phamviet86/local-rag-mcp.git
cd local-rag-mcp
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

If a change uses optional Drive or local-embedding code, install the matching extra explicitly in the
development environment. Do not add an actual OAuth token, client secret, source folder, Drive root,
or remote-embedding key merely to make a test pass.

```bash
python -m pip install -e '.[dev,google-drive]'
# Or:
python -m pip install -e '.[dev,local-embeddings]'
```

## Change and validate

Work through the CLI or MCP boundary; internal modules are not a stable public integration API. Keep
the zero-source state valid, preserve source authority, and do not loosen reader/reviewer/admin
capability boundaries. Update the relevant operator, agent, security, or release documentation when
the public behavior changes.

```bash
ruff format --check .
ruff check .
mypy
pytest
python -m compileall -q src tests scripts
```

For a packaging or CLI/MCP change, also build and check the distribution in a clean environment as
described by the maintainer checklist in [docs/deployment.md](docs/deployment.md#maintainer-checklist-for-future-releases).
Never commit secrets, publish a release, push a branch, or alter a user's source/data root without
explicit authority.
