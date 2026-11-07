from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from local_rag.config import Settings
from local_rag.mcp import create_http_app, is_loopback_host, validate_http_options
from local_rag.service import MultiSourceRAG

TOKEN = "t" * 24
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class HTTPTransportTests(unittest.TestCase):
    def test_option_validation(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1") and is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        validate_http_options("127.0.0.1", "reader", None)
        with self.assertRaises(ValueError):
            validate_http_options("0.0.0.0", "reader", None)
        with self.assertRaises(ValueError):
            validate_http_options("127.0.0.1", "admin", None)
        with self.assertRaises(ValueError):
            validate_http_options("127.0.0.1", "reader", "short")
        validate_http_options("0.0.0.0", "reader", TOKEN)

    def test_bearer_token_enforced_and_initialize_works(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = Settings(root=home, home=home, ocr_mode="no-ocr")
            settings.save()
            app = create_http_app(MultiSourceRAG(settings), token=TOKEN)
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                denied = client.post("/mcp", json=INIT, headers=HEADERS)
                self.assertEqual(denied.status_code, 401)
                wrong = client.post(
                    "/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer nope"}
                )
                self.assertEqual(wrong.status_code, 401)
                ok = client.post(
                    "/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"}
                )
                self.assertEqual(ok.status_code, 200, ok.text)
                self.assertIn("local-rag-mcp", ok.text)

    def test_loopback_without_token_allows_reader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = Settings(root=home, home=home, ocr_mode="no-ocr")
            settings.save()
            app = create_http_app(MultiSourceRAG(settings))
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                self.assertEqual(client.post("/mcp", json=INIT, headers=HEADERS).status_code, 200)
                evil = client.post(
                    "/mcp", json=INIT, headers={**HEADERS, "Host": "evil.example:8765"}
                )
                self.assertEqual(evil.status_code, 421)


if __name__ == "__main__":
    unittest.main()
