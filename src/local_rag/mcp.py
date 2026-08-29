import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, TextIO

from .config import Settings
from .search import SEARCH_MODES
from .service import LocalRAG


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


READER_TOOLS = [
    {
        "name": "search",
        "description": "Search indexed content and metadata globally or within a subfolder.",
        "inputSchema": _schema(
            {
                "query": {"type": "string"},
                "scope": {"type": "string"},
                "mode": {"type": "string", "enum": list(SEARCH_MODES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["query"],
        ),
    },
    {
        "name": "read",
        "description": "Read indexed extracted text with source provenance.",
        "inputSchema": _schema(
            {
                "path": {"type": "string"},
                "start": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            ["path"],
        ),
    },
    {
        "name": "status",
        "description": "Inspect index and provider status.",
        "inputSchema": _schema({}),
    },
    {
        "name": "metadata",
        "description": "Read automatic/agent metadata and relationships for a document.",
        "inputSchema": _schema({"path": {"type": "string"}}, ["path"]),
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
                "source": {"type": "string"},
                "target": {"type": "string"},
                "relation": {"type": "string"},
                "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "actor": {"type": "string"},
            },
            ["source", "target", "relation", "evidence", "actor"],
        ),
    },
]

ADMIN_TOOLS = [
    {
        "name": "reconcile",
        "description": "Reconcile a target or the complete configured root.",
        "inputSchema": _schema({"target": {"type": "string"}}),
    },
    {
        "name": "reindex",
        "description": (
            "Rebuild index state from cached extraction for a target or the complete root. "
            "Set reextract only to deliberately rerun extraction/OCR."
        ),
        "inputSchema": _schema(
            {
                "target": {"type": "string"},
                "reextract": {
                    "type": "boolean",
                    "default": False,
                    "description": "Rerun extraction/OCR instead of using cached artifacts.",
                },
            }
        ),
    },
]


class MCPServer:
    def __init__(self, service: LocalRAG, mode: str = "reader"):
        if mode not in {"reader", "reviewer", "admin"}:
            raise ValueError("MCP mode must be reader, reviewer, or admin")
        self.service = service
        self.mode = mode
        self.tools = [*READER_TOOLS]
        if mode in {"reviewer", "admin"}:
            self.tools.extend(REVIEWER_TOOLS)
        if mode == "admin":
            self.tools.extend(ADMIN_TOOLS)
        self.allowed = {tool["name"] for tool in self.tools}

    def call(self, name: str, values: Dict[str, Any]) -> Any:
        if name not in self.allowed:
            raise ValueError(f"tool is not exposed in {self.mode} mode: {name}")
        handlers: Dict[str, Callable[[], Any]] = {
            "search": lambda: self.service.search(
                values["query"],
                int(values.get("limit", 8)),
                values.get("scope"),
                values.get("mode", "hybrid"),
            ),
            "read": lambda: self.service.read(
                values["path"], int(values.get("start", 0)), int(values.get("length", 12000))
            ),
            "status": self.service.status,
            "metadata": lambda: self.service.metadata(values["path"]),
            "reviews": lambda: self.service.reviews(values.get("status", "open")),
            "correct_page": lambda: self.service.correct_review(
                int(values["review_id"]),
                values["text"],
                values["evidence"],
                values["actor"],
            ),
            "resolve_review": lambda: self.service.resolve_review(
                int(values["review_id"]), values["resolution"]
            ),
            "add_metadata": lambda: self.service.add_metadata(
                values["path"],
                values["key"],
                values["value"],
                values["evidence"],
                values["actor"],
            ),
            "add_relationship": lambda: self.service.add_relationship(
                values["source"],
                values["target"],
                values["relation"],
                values["evidence"],
                values["actor"],
            ),
            "reconcile": lambda: self.service.scan(values.get("target")),
            "reindex": lambda: self.service.scan(
                values.get("target"),
                force_index=True,
                reextract=bool(values.get("reextract", False)),
            ),
        }
        return handlers[name]()

    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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
                    "serverInfo": {"name": "local-rag", "version": "0.3.2"},
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
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            return _error(request_id, -32603, str(exc))


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


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> int:
    mode = os.environ.get("LOCAL_RAG_MCP_MODE", "reader")
    return serve(LocalRAG(Settings.load()), mode=mode)


if __name__ == "__main__":
    raise SystemExit(main())
