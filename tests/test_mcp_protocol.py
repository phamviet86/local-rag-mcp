from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import anyio
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult

from local_rag.config import Settings
from local_rag.mcp import create_sdk_server
from local_rag.service import MultiSourceRAG


class MCPContractTests(unittest.TestCase):
    def test_native_sdk_errors_annotations_and_structured_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = Settings(root=home, home=home, ocr_mode="no-ocr")
            settings.save()
            server = create_sdk_server(MultiSourceRAG(settings))

            async def check() -> None:
                tools = {tool.name: tool for tool in await server.list_tools()}
                self.assertTrue(tools["search"].annotations.open_world_hint)
                self.assertTrue(tools["search"].annotations.read_only_hint)
                self.assertNotIn("remove_source", tools)
                search = await server.call_tool("search", {"query": "fixture"})
                self.assertIsInstance(search, CallToolResult)
                self.assertTrue(search.is_error)
                self.assertEqual(search.structured_content["error"]["code"], "no_enabled_sources")
                self.assertTrue(search.content)
                status = await server.call_tool("status", {})
                self.assertFalse(status.is_error)
                self.assertEqual(status.structured_content["error"]["code"], "no_enabled_sources")
                doctor = await server.call_tool("doctor", {})
                self.assertFalse(doctor.is_error)
                with self.assertRaises(MCPError) as error:
                    await server.call_tool("remove_source", {"source": "unknown"})
                self.assertEqual(error.exception.error.code, INVALID_PARAMS)

            anyio.run(check)


if __name__ == "__main__":
    unittest.main()
