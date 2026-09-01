#!/usr/bin/env python3
"""Exercise an installed local-rag-mcp server over the real MCP stdio transport.

This intentionally uses only the standard library so release CI can prove that
the console script from a freshly installed wheel, rather than the checkout,
starts and answers the reader-safe MCP calls needed by a new installation.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser


class MCPClient:
    def __init__(self, executable: Path, home: Path, timeout: float) -> None:
        self.timeout = timeout
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [str(executable), "--home", str(home), "mcp", "--profile", "reader"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def request(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            wait_for = max(deadline - time.monotonic(), 0.01)
            readable, _, _ = select.select([self.process.stdout], [], [], wait_for)
            if not readable:
                continue
            line = self.process.stdout.readline()
            if not line:
                break
            response = json.loads(line)
            if response.get("id") == request_id:
                if "error" in response:
                    raise RuntimeError(f"MCP {method} failed: {response['error']}")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError(f"MCP {method} returned a non-object result: {response}")
                return result
        self.close()
        stderr_stream = self.process.stderr
        assert stderr_stream is not None
        stderr = stderr_stream.read()
        raise RuntimeError(f"timed out waiting for MCP {method}; stderr: {stderr}")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()


def _call(client: MCPClient, request_id: int, name: str) -> dict[str, Any]:
    result = client.request(
        request_id,
        "tools/call",
        {"name": name, "arguments": {}},
    )
    if result.get("isError"):
        raise RuntimeError(f"MCP {name} tool returned an error: {result}")
    return result


def main() -> int:
    args = _parser().parse_args()
    client = MCPClient(args.executable, args.home, args.timeout)
    try:
        initialized = client.request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "release-ci", "version": "1"},
            },
        )
        if initialized.get("serverInfo", {}).get("name") != "local-rag-mcp":
            raise RuntimeError(f"unexpected server identity: {initialized.get('serverInfo')}")
        client.notify("notifications/initialized", {})
        listed = client.request(2, "tools/list", {})
        names = {tool.get("name") for tool in listed.get("tools", [])}
        required = {"status", "doctor", "sources"}
        if missing := required - names:
            raise RuntimeError(f"reader tool list is missing: {sorted(missing)}")
        _call(client, 3, "status")
        doctor = _call(client, 4, "doctor")
        content = doctor.get("content", [])
        if not content or content[0].get("type") != "text":
            raise RuntimeError(f"doctor response is not text content: {doctor}")
        payload = json.loads(content[0]["text"])
        if payload.get("status") not in {"blocked", "degraded"}:
            raise RuntimeError(f"unexpected fresh-install doctor status: {payload}")
        print("MCP stdio release smoke passed")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
