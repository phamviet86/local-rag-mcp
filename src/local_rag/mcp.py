from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from . import __version__
from .config import Settings
from .search import SEARCH_MODES
from .service import LocalRAG, MultiSourceRAG


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


READER_TOOLS = [
    {
        "name": "search",
        "description": "Search all enabled sources, optionally filtered by source and folder.",
        "inputSchema": _schema(
            {
                "query": {"type": "string"},
                "source": {"type": "string"},
                "folder": {"type": "string"},
                "mode": {"type": "string", "enum": list(SEARCH_MODES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["query"],
        ),
    },
    {
        "name": "read",
        "description": (
            "Read cached indexed text plus source identity, authority, revision/hash, "
            "and provenance."
        ),
        "inputSchema": _schema(
            {
                "path": {"type": "string"},
                "source": {"type": "string"},
                "start": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            ["path"],
        ),
    },
    {
        "name": "status",
        "description": "Inspect source/index/provider status.",
        "inputSchema": _schema({}),
    },
    {"name": "sources", "description": "List configured sources.", "inputSchema": _schema({})},
    {
        "name": "metadata",
        "description": "Read automatic/agent metadata and document relationships.",
        "inputSchema": _schema(
            {"path": {"type": "string"}, "source": {"type": "string"}}, ["path"]
        ),
    },
    {
        "name": "reviews",
        "description": "List durable PDF review items.",
        "inputSchema": _schema({"status": {"type": "string", "enum": ["open", "resolved"]}}),
    },
]

REVIEWER_TOOLS = [
    {
        "name": "correct_page",
        "description": "Submit corrected PDF page text with evidence and actor.",
        "inputSchema": _schema(
            {
                "review_id": {"type": "integer"},
                "text": {"type": "string"},
                "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "actor": {"type": "string"},
            },
            ["review_id", "text", "evidence", "actor"],
        ),
    },
    {
        "name": "resolve_review",
        "description": "Resolve a review item without replacing page text.",
        "inputSchema": _schema(
            {"review_id": {"type": "integer"}, "resolution": {"type": "string"}},
            ["review_id", "resolution"],
        ),
    },
    {
        "name": "add_metadata",
        "description": "Add evidence-backed document metadata.",
        "inputSchema": _schema(
            {
                "path": {"type": "string"},
                "source": {"type": "string"},
                "key": {"type": "string"},
                "value": {},
                "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "actor": {"type": "string"},
            },
            ["path", "key", "value", "evidence", "actor"],
        ),
    },
    {
        "name": "add_relationship",
        "description": "Add an evidence-backed relationship between indexed documents.",
        "inputSchema": _schema(
            {
                "source_path": {"type": "string"},
                "target_path": {"type": "string"},
                "relation": {"type": "string"},
                "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "actor": {"type": "string"},
                "source_source": {"type": "string"},
                "target_source": {"type": "string"},
            },
            ["source_path", "target_path", "relation", "evidence", "actor"],
        ),
    },
]

ADMIN_TOOLS = [
    {
        "name": "reconcile",
        "description": "Incrementally reconcile one source or every enabled source.",
        "inputSchema": _schema(
            {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "full": {"type": "boolean"},
            }
        ),
    },
    {
        "name": "reindex",
        "description": "Rebuild cached index state; reextract is explicit and defaults false.",
        "inputSchema": _schema(
            {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "reextract": {"type": "boolean", "default": False},
                "all": {"type": "boolean", "default": False},
            }
        ),
    },
    {
        "name": "set_source_enabled",
        "description": "Enable or disable a registered source without deleting index data.",
        "inputSchema": _schema(
            {"source": {"type": "string"}, "enabled": {"type": "boolean"}},
            ["source", "enabled"],
        ),
    },
    {
        "name": "remove_source",
        "description": "Purge only one source's local index/cache. Never deletes source files.",
        "inputSchema": _schema(
            {"source": {"type": "string"}, "confirm": {"type": "boolean"}},
            ["source", "confirm"],
        ),
    },
]


class MCPServer:
    """Deterministic dispatcher retained for compatibility and protocol-level tests."""

    def __init__(self, service: Any, mode: str = "reader"):
        if mode not in {"reader", "reviewer", "admin"}:
            raise ValueError("MCP profile must be reader, reviewer, or admin")
        self.service, self.mode = service, mode
        self.tools = [*READER_TOOLS]
        if mode in {"reviewer", "admin"}:
            self.tools.extend(REVIEWER_TOOLS)
        if mode == "admin":
            self.tools.extend(ADMIN_TOOLS)
        if not isinstance(service, MultiSourceRAG):
            self.tools = [tool for tool in self.tools if tool["name"] != "sources"]
        self.allowed = {tool["name"] for tool in self.tools}

    def call(self, name: str, values: dict[str, Any]) -> Any:
        if name not in self.allowed:
            raise ValueError(f"tool is not exposed in {self.mode} profile: {name}")
        multi = isinstance(self.service, MultiSourceRAG)
        if name == "search":
            if multi:
                return self.service.search(
                    values["query"],
                    int(values.get("limit", 8)),
                    values.get("source"),
                    values.get("folder"),
                    values.get("mode", "hybrid"),
                )
            return self.service.search(
                values["query"],
                int(values.get("limit", 8)),
                values.get("folder") or values.get("scope"),
                values.get("mode", "hybrid"),
            )
        if name == "read":
            if multi:
                return self.service.read(
                    values["path"],
                    values.get("source"),
                    int(values.get("start", 0)),
                    int(values.get("length", 12000)),
                )
            return self.service.read(
                values["path"], int(values.get("start", 0)), int(values.get("length", 12000))
            )
        if name == "status":
            return self.service.status()
        if name == "sources":
            return self.service.sources()
        if name == "metadata":
            return (
                self.service.metadata(values["path"], values.get("source"))
                if multi
                else self.service.metadata(values["path"])
            )
        if name == "reviews":
            return self.service.reviews(values.get("status", "open"))
        if name == "correct_page":
            return self.service.correct_review(
                int(values["review_id"]), values["text"], values["evidence"], values["actor"]
            )
        if name == "resolve_review":
            return self.service.resolve_review(int(values["review_id"]), values["resolution"])
        if name == "add_metadata":
            arguments = (
                values["path"],
                values["key"],
                values["value"],
                values["evidence"],
                values["actor"],
            )
            return (
                self.service.add_metadata(*arguments, values.get("source"))
                if multi
                else self.service.add_metadata(*arguments)
            )
        if name == "add_relationship":
            arguments = (
                values["source_path"],
                values["target_path"],
                values["relation"],
                values["evidence"],
                values["actor"],
            )
            return (
                self.service.add_relationship(
                    *arguments, values.get("source_source"), values.get("target_source")
                )
                if multi
                else self.service.add_relationship(*arguments)
            )
        if name == "reconcile":
            return (
                self.service.reconcile(
                    values.get("source"), values.get("target"), full=bool(values.get("full", False))
                )
                if multi
                else self.service.scan(values.get("target"))
            )
        if name == "reindex":
            return (
                self.service.reconcile(
                    values.get("source"),
                    values.get("target"),
                    force_index=True,
                    reextract=bool(values.get("reextract", False)),
                    full=bool(values.get("all", False)),
                )
                if multi
                else self.service.scan(
                    values.get("target"),
                    force_index=True,
                    reextract=bool(values.get("reextract", False)),
                )
            )
        if name == "set_source_enabled":
            return self.service.enable_source(values["source"], bool(values["enabled"]))
        if name == "remove_source":
            if values.get("confirm") is not True:
                raise ValueError("remove_source requires confirm=true")
            return self.service.remove_source(values["source"])
        raise ValueError(f"unknown tool: {name}")

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id, method = request.get("id"), request.get("method")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": request.get("params", {}).get(
                        "protocolVersion", "2024-11-05"
                    ),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "local-rag-mcp", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools}
            elif method == "tools/call":
                params = request.get("params") or {}
                value = self.call(params.get("name", ""), params.get("arguments") or {})
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
                }
            else:
                return _error(request_id, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            if method == "tools/call":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
                }
            return _error(request_id, -32603, str(exc))


def create_sdk_server(service: MultiSourceRAG, profile: str = "reader") -> Any:
    try:
        from mcp.server.mcpserver import MCPServer as SDKServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError("MCP SDK is not installed; install local-rag-mcp") from exc
    dispatcher = MCPServer(service, profile)
    server = SDKServer(
        "local-rag-mcp",
        version=__version__,
        instructions=(
            "Search all enabled local and Google Drive sources by default. Cite source, path, URL, "
            "content hash, page, and locator. Hybrid search uses reciprocal-rank fusion and falls "
            "back to full text when embeddings fail. Mutation tools exist only in reviewer/admin "
            "profiles."
        ),
    )
    read_only = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    mutate = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )
    destructive = ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
    )

    @server.tool(annotations=read_only)
    def search(
        query: str,
        source: str | None = None,
        folder: str | None = None,
        mode: str = "hybrid",
        limit: int = 8,
    ) -> dict[str, Any]:
        """Search globally or filter by source and relative folder."""
        return dispatcher.call(
            "search",
            {"query": query, "source": source, "folder": folder, "mode": mode, "limit": limit},
        )

    @server.tool(annotations=read_only)
    def read(
        path: str,
        source: str | None = None,
        start: int = 0,
        length: int = 12000,
    ) -> dict[str, Any]:
        """Read cached text with source identity, authority, revision/hash, and provenance."""
        return dispatcher.call(
            "read", {"path": path, "source": source, "start": start, "length": length}
        )

    @server.tool(annotations=read_only)
    def status() -> dict[str, Any]:
        """Inspect source, index, embedding, OCR, and sync status."""
        return dispatcher.call("status", {})

    @server.tool(annotations=read_only)
    def sources() -> list[dict[str, Any]]:
        """List configured local and Google Drive sources."""
        return dispatcher.call("sources", {})

    @server.tool(annotations=read_only)
    def metadata(path: str, source: str | None = None) -> dict[str, Any]:
        """Read automatic and evidence-backed metadata and relationships."""
        return dispatcher.call("metadata", {"path": path, "source": source})

    @server.tool(annotations=read_only)
    def reviews(status: str = "open") -> list[dict[str, Any]]:
        """List durable OCR review items."""
        return dispatcher.call("reviews", {"status": status})

    if profile in {"reviewer", "admin"}:

        @server.tool(annotations=mutate)
        def correct_page(
            review_id: int,
            text: str,
            evidence: list[dict[str, Any]],
            actor: str,
        ) -> dict[str, Any]:
            """Correct one reviewed PDF page without replacing the base artifact."""
            return dispatcher.call(
                "correct_page",
                {"review_id": review_id, "text": text, "evidence": evidence, "actor": actor},
            )

        @server.tool(annotations=mutate)
        def resolve_review(review_id: int, resolution: str) -> dict[str, Any]:
            """Resolve an OCR review item without changing its page text."""
            return dispatcher.call(
                "resolve_review", {"review_id": review_id, "resolution": resolution}
            )

        @server.tool(annotations=mutate)
        def add_metadata(
            path: str,
            key: str,
            value: Any,
            evidence: list[dict[str, Any]],
            actor: str,
            source: str | None = None,
        ) -> dict[str, Any]:
            """Add evidence-backed metadata to an indexed document."""
            return dispatcher.call(
                "add_metadata",
                {
                    "path": path,
                    "source": source,
                    "key": key,
                    "value": value,
                    "evidence": evidence,
                    "actor": actor,
                },
            )

        @server.tool(annotations=mutate)
        def add_relationship(
            source_path: str,
            target_path: str,
            relation: str,
            evidence: list[dict[str, Any]],
            actor: str,
            source_source: str | None = None,
            target_source: str | None = None,
        ) -> dict[str, Any]:
            """Add an evidence-backed relationship between indexed documents."""
            return dispatcher.call(
                "add_relationship",
                {
                    "source_path": source_path,
                    "target_path": target_path,
                    "relation": relation,
                    "evidence": evidence,
                    "actor": actor,
                    "source_source": source_source,
                    "target_source": target_source,
                },
            )

    if profile == "admin":

        @server.tool(annotations=mutate)
        def reconcile(
            source: str | None = None,
            target: str | None = None,
            full: bool = False,
        ) -> dict[str, Any]:
            """Incrementally reconcile one source or every enabled source."""
            return dispatcher.call("reconcile", {"source": source, "target": target, "full": full})

        @server.tool(annotations=mutate)
        def reindex(
            source: str | None = None,
            target: str | None = None,
            reextract: bool = False,
            all: bool = False,
        ) -> dict[str, Any]:
            """Rebuild cached index state, optionally rerunning extraction/OCR."""
            return dispatcher.call(
                "reindex",
                {"source": source, "target": target, "reextract": reextract, "all": all},
            )

        @server.tool(annotations=mutate)
        def set_source_enabled(source: str, enabled: bool) -> dict[str, Any]:
            """Enable or disable a source without deleting indexed data."""
            return dispatcher.call("set_source_enabled", {"source": source, "enabled": enabled})

        @server.tool(annotations=destructive)
        def remove_source(source: str, confirm: bool = False) -> dict[str, Any]:
            """Purge only a source's local index/cache; never source files."""
            return dispatcher.call("remove_source", {"source": source, "confirm": confirm})

    return server


def run_sdk_server(service: MultiSourceRAG, profile: str = "reader") -> int:
    create_sdk_server(service, profile).run(transport="stdio")
    return 0


def serve(
    service: LocalRAG,
    mode: str = "reader",
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    server = MCPServer(service, mode)
    for line in input_stream:
        if not line.strip():
            continue
        try:
            response = server.handle(json.loads(line))
        except Exception as exc:
            response = _error(None, -32700, f"parse error: {exc}")
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()
    return 0


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> int:
    profile = os.environ.get("LOCAL_RAG_MCP_PROFILE") or os.environ.get(
        "LOCAL_RAG_MCP_MODE", "reader"
    )
    return run_sdk_server(MultiSourceRAG(Settings.load()), profile)


if __name__ == "__main__":
    raise SystemExit(main())
