import json
import sys
from typing import Any, Callable, Dict, List, Optional, TextIO

from .config import Settings
from .service import LocalRAG


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS = [
    {
        "name": "local_rag_search",
        "description": "READ: search globally or within a configured-root subfolder.",
        "inputSchema": _schema(
            {
                "query": {"type": "string"},
                "scope": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            ["query"],
        ),
    },
    {
        "name": "local_rag_read",
        "description": "READ: read extracted text and source-position provenance for an indexed file.",
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
        "name": "local_rag_status",
        "description": "READ: inspect index, job, review, embedding, and OCR status.",
        "inputSchema": _schema({}),
    },
    {
        "name": "local_rag_review_list",
        "description": "READ: list durable PDF quality-review items.",
        "inputSchema": _schema({"status": {"type": "string", "enum": ["open", "resolved"]}}),
    },
    {
        "name": "local_rag_metadata_get",
        "description": "READ: get automatic metadata, agent metadata, and document relationships.",
        "inputSchema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    {
        "name": "local_rag_admin_reconcile",
        "description": "ADMIN: reconcile a file/folder or the complete configured root.",
        "inputSchema": _schema({"target": {"type": "string"}}),
    },
    {
        "name": "local_rag_admin_reindex",
        "description": "ADMIN: force rebuild a file/folder or all indexed content.",
        "inputSchema": _schema({"target": {"type": "string"}}),
    },
    {
        "name": "local_rag_admin_review_resolve",
        "description": "ADMIN: resolve a durable review item with an operator explanation.",
        "inputSchema": _schema(
            {"id": {"type": "integer"}, "resolution": {"type": "string"}},
            ["id", "resolution"],
        ),
    },
    {
        "name": "local_rag_admin_metadata_add",
        "description": "ADMIN: add metadata; source evidence is mandatory.",
        "inputSchema": _schema(
            {
                "path": {"type": "string"},
                "key": {"type": "string"},
                "value": {},
                "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "actor": {"type": "string"},
            },
            ["path", "key", "value", "evidence"],
        ),
    },
    {
        "name": "local_rag_admin_relationship_add",
        "description": "ADMIN: add an evidence-backed relationship between indexed documents.",
        "inputSchema": _schema(
            {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "relation": {"type": "string"},
                "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "actor": {"type": "string"},
            },
            ["source", "target", "relation", "evidence"],
        ),
    },
]


class MCPServer:
    def __init__(self, service: LocalRAG):
        self.service = service

    def call(self, name: str, values: Dict[str, Any]) -> Any:
        handlers: Dict[str, Callable[[], Any]] = {
            "local_rag_search": lambda: self.service.search(
                values["query"], int(values.get("limit", 8)), values.get("scope")
            ),
            "local_rag_read": lambda: self.service.read(
                values["path"], int(values.get("start", 0)), int(values.get("length", 12000))
            ),
            "local_rag_status": self.service.status,
            "local_rag_review_list": lambda: self.service.reviews(values.get("status", "open")),
            "local_rag_metadata_get": lambda: self.service.metadata(values["path"]),
            "local_rag_admin_reconcile": lambda: self.service.scan(values.get("target")),
            "local_rag_admin_reindex": lambda: self.service.scan(values.get("target"), force=True),
            "local_rag_admin_review_resolve": lambda: self.service.resolve_review(
                int(values["id"]), values["resolution"]
            ),
            "local_rag_admin_metadata_add": lambda: self.service.add_metadata(
                values["path"],
                values["key"],
                values["value"],
                values["evidence"],
                values.get("actor", "mcp-agent"),
            ),
            "local_rag_admin_relationship_add": lambda: self.service.add_relationship(
                values["source"],
                values["target"],
                values["relation"],
                values["evidence"],
                values.get("actor", "mcp-agent"),
            ),
        }
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        if "evidence" in values and not values["evidence"]:
            raise ValueError("evidence is required")
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
                    "serverInfo": {"name": "local-rag", "version": "0.2.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
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
    service: LocalRAG, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout
) -> int:
    server = MCPServer(service)
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
    return serve(LocalRAG(Settings.load()))


if __name__ == "__main__":
    raise SystemExit(main())
