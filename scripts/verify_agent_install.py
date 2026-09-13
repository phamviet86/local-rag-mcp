#!/usr/bin/env python3
"""Verify a separately installed wheel's skills and real SDK retrieval, using only fixture data.

Run with the clean environment's Python from outside the source checkout. This script imports the
MCP SDK, never local-rag internal modules. Temporary runtime/skills/source state is removed on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult


async def verify(executable: Path, home: Path, env: dict[str, str], modern: bool) -> None:
    parameters = StdioServerParameters(
        command=str(executable),
        args=["--home", str(home), "mcp", "--profile", "reader"],
        env=env,
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream, read_timeout_seconds=30) as session,
    ):
        if modern:
            discovered = await session.discover()
            assert "2026-07-28" in discovered.supported_versions
            assert session.protocol_version == "2026-07-28"
        else:
            initialized = await session.initialize()
            assert initialized.protocol_version == "2025-11-25"
        assert session.server_info and session.server_info.name == "local-rag-mcp"
        listed = await session.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        assert {"doctor", "sources", "search", "read", "index_coverage"} <= tools.keys()
        assert "remove_source" not in tools
        assert tools["search"].annotations and tools["search"].annotations.open_world_hint
        assert tools["search"].input_schema["type"] == "object"

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await session.call_tool(name, arguments)
            assert isinstance(result, CallToolResult)
            assert not result.is_error, result
            assert isinstance(result.structured_content, dict)
            assert result.content
            return result.structured_content

        await call("doctor", {})
        await call("sources", {})
        search = await call(
            "search", {"query": "orchard", "source": "fixture", "mode": "full_text"}
        )
        assert search["results"] and "coverage" in search
        document = await call("read", {"path": search["results"][0]["document_ref"]})
        assert "orchard" in json.dumps(document).lower()
        assert document.get("provenance")
        try:
            await session.call_tool("absent-tool", {})
        except MCPError as exc:
            assert exc.error.code == INVALID_PARAMS
        else:
            raise AssertionError("unknown tools must return JSON-RPC invalid params")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.absolute()
    env = {key: value for key, value in os.environ.items() if not key.startswith("LOCAL_RAG_")}
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="rag-wheel-agent-") as directory:
        base = Path(directory)
        home, source, skills = base / "data", base / "documents", base / "skills with spaces"
        source.mkdir()
        (source / "fixture.md").write_text("# Orchard\nThe orchard harvest code is green-47.\n")

        def run(*arguments: str) -> dict[str, Any]:
            result = subprocess.run(
                [str(executable), "--home", str(home), *arguments],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            return json.loads(result.stdout)

        run("install-skills", "--dest", str(skills), "--dry-run")
        assert not skills.exists() and not home.exists()
        run("install-skills", "--dest", str(skills))
        run("install-skills", "--dest", str(skills), "--check")
        for name in ("local-rag-setup", "local-rag"):
            assert (skills / name / "SKILL.md").is_file()
            runtime = json.loads((skills / name / "references/runtime.json").read_text())
            assert Path(runtime["cli"]) == executable
        run("setup", "--no-ocr")
        run("source", "add-local", "fixture", str(source))
        run("reconcile", "--source", "fixture")
        for modern in (True, False):
            anyio.run(verify, executable, home, env, modern)
    print("Installed-wheel skills and SDK search/read passed (2026-07-28 + 2025-11-25)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
