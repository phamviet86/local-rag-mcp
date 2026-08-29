import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileOpenedEvent,
)

from local_rag.cli import parser
from local_rag.config import Settings
from local_rag.mcp import MCPServer
from local_rag.service import LocalRAG
from local_rag.watcher import CoalescingEventHandler, WatchService
from tests.helpers import write_text_pdf


class FakeEmbeddings:
    identity = ("test-local", "tiny-v1")

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [
            [float("orchard" in text.lower()), float("ocean" in text.lower())] for text in texts
        ]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.home = base / "root", base / "home"
        self.root.mkdir()
        self.settings = Settings(root=self.root, home=self.home, chunk_chars=100, chunk_overlap=10)
        self.settings.save()

    def tearDown(self):
        self.temp.cleanup()

    def test_reconcile_scoped_global_vectors_metadata_relationship_rebuild_and_remove(self):
        team_a, team_b = self.root / "team-a", self.root / "team-b"
        team_a.mkdir()
        team_b.mkdir()
        first, second = team_a / "one.md", team_b / "two.txt"
        first.write_text("# Orchard\nshared apple decision", encoding="utf-8")
        second.write_text("shared ocean decision", encoding="utf-8")
        embeddings = FakeEmbeddings()
        service = LocalRAG(self.settings, embeddings)
        report = service.scan()
        self.assertEqual(report["indexed"], 2)
        self.assertEqual(len(service.search("shared")["results"]), 2)
        scoped = service.search("shared", scope="team-a")
        self.assertEqual([hit["path"] for hit in scoped["results"]], ["team-a/one.md"])
        self.assertEqual(service.search("orchard")["results"][0]["path"], "team-a/one.md")
        self.assertTrue(service.read("team-a/one.md")["provenance"])

        evidence = [{"path": "team-a/one.md", "locator": "line:2", "quote": "apple decision"}]
        service.add_metadata("team-a/one.md", "decision", "approved", evidence, "test")
        service.add_relationship("team-a/one.md", "team-b/two.txt", "related", evidence, "test")
        metadata = service.metadata("team-a/one.md")
        self.assertEqual(metadata["agent"][0]["key"], "decision")
        self.assertEqual(metadata["relationships"][0]["relation"], "related")

        calls_before_rebuild = embeddings.calls
        rebuilt = service.scan("team-a/one.md", force_index=True)
        self.assertEqual(rebuilt["indexed"], 1)
        self.assertEqual(embeddings.calls, calls_before_rebuild)
        first.write_text("# Orchard\nupdated harvest", encoding="utf-8")
        self.assertEqual(service.scan("team-a/one.md")["indexed"], 1)
        self.assertTrue(service.search("harvest")["results"])
        second.unlink()
        self.assertEqual(service.scan()["removed"], 1)
        self.assertEqual(service.status()["documents"], 1)

    def test_watcher_create_modify_move_delete(self):
        service = LocalRAG(self.settings)
        watcher = WatchService(service)
        self.assertNotIn("polling", watcher.observer.__class__.__module__)
        note = self.root / "watched.txt"
        note.write_text("stable content", encoding="utf-8")
        handler = CoalescingEventHandler(service, stabilize_seconds=10, max_pending=4)
        service.indexer.index_paths = MagicMock(
            return_value={"changed": 1, "embedded": 0, "warnings": [], "errors": []}
        )
        handler.on_any_event(FileCreatedEvent(str(note)))
        for _ in range(5):
            handler.on_any_event(FileModifiedEvent(str(note)))
        handler.on_any_event(FileOpenedEvent(str(note)))
        self.assertEqual(handler.pending_count, 1)
        result = handler.flush_ready(force=True)
        self.assertEqual(result["changed"], 1)
        service.indexer.index_paths.assert_called_once()
        moved = self.root / "moved.txt"
        deleted = self.root / "deleted.txt"
        service.indexer.move = MagicMock(return_value=True)
        service.indexer.remove = MagicMock(return_value=True)
        handler.on_any_event(FileMovedEvent(str(note), str(moved)))
        handler.on_any_event(FileDeletedEvent(str(deleted)))
        moved_result = handler.flush_ready(force=True)
        self.assertEqual(moved_result["changed"], 2)
        service.indexer.move.assert_called_once()
        service.indexer.remove.assert_called_once()
        for index in range(8):
            handler.on_any_event(FileModifiedEvent(str(self.root / f"{index}.txt")))
        self.assertEqual(handler.pending_count, 4)

    def test_mcp_read_and_admin_workflows(self):
        (self.root / "note.txt").write_text("MCP searchable content", encoding="utf-8")
        service = LocalRAG(self.settings)
        service.scan()
        server = MCPServer(service, mode="reader")
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "local-rag")
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("read", names)
        self.assertNotIn("reindex", names)
        called = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "searchable"}},
            }
        )
        payload = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(payload["results"][0]["path"], "note.txt")
        reviewer = MCPServer(service, mode="reviewer")
        failed = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "add_metadata",
                    "arguments": {"path": "note.txt", "key": "x", "value": 1, "evidence": []},
                },
            }
        )
        self.assertTrue(failed["result"]["isError"])
        self.assertIn("correct_page", reviewer.allowed)
        self.assertNotIn("reconcile", reviewer.allowed)
        admin = MCPServer(service, mode="admin")
        self.assertIn("reconcile", admin.allowed)
        reindex = next(tool for tool in admin.tools if tool["name"] == "reindex")
        self.assertFalse(reindex["inputSchema"]["properties"]["reextract"]["default"])
        with patch.object(
            service.extractor, "extract", side_effect=AssertionError("extractor reran")
        ) as extracted:
            rebuilt = admin.call("reindex", {"target": "note.txt"})
        self.assertEqual(rebuilt["indexed"], 1)
        extracted.assert_not_called()
        with patch.object(
            service.extractor, "extract", wraps=service.extractor.extract
        ) as extracted:
            rebuilt = admin.call("reindex", {"target": "note.txt", "reextract": True})
        self.assertEqual(rebuilt["indexed"], 1)
        extracted.assert_called_once()
        self.assertFalse(parser().parse_args(["reindex", "--all"]).reextract)
        self.assertTrue(
            parser().parse_args(["reindex", "--target", "note.txt", "--reextract"]).reextract
        )

    def test_pdf_artifact_and_review_are_durable(self):
        pdf = self.root / "document.pdf"
        write_text_pdf(pdf, "Cached PDF content")
        service = LocalRAG(self.settings)
        report = service.scan()
        self.assertEqual(report["indexed"], 1)
        document = service.db.resolve_document("document.pdf")
        artifact = Path(document["artifact_path"])
        self.assertEqual(artifact.stem, document["content_hash"])
        self.assertTrue(artifact.exists())
        reviews = service.reviews()
        self.assertEqual(reviews[0]["path"], str(pdf.resolve()))
        second = service.scan()
        self.assertEqual(second["unchanged"], 1)


if __name__ == "__main__":
    unittest.main()
