import json
import tempfile
import time
import unittest
from pathlib import Path

from local_rag.config import Settings
from local_rag.mcp import MCPServer
from local_rag.service import LocalRAG
from local_rag.watcher import WatchService
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
        self.assertEqual(len(service.search("shared")), 2)
        scoped = service.search("shared", scope="team-a")
        self.assertEqual([hit["path"] for hit in scoped], ["team-a/one.md"])
        self.assertEqual(service.search("orchard")[0]["path"], "team-a/one.md")
        self.assertTrue(service.read("team-a/one.md")["provenance"])

        evidence = [{"path": "team-a/one.md", "locator": "line:2", "quote": "apple decision"}]
        service.add_metadata("team-a/one.md", "decision", "approved", evidence, "test")
        service.add_relationship("team-a/one.md", "team-b/two.txt", "related", evidence, "test")
        metadata = service.metadata("team-a/one.md")
        self.assertEqual(metadata["agent"][0]["key"], "decision")
        self.assertEqual(metadata["relationships"][0]["relation"], "related")

        calls_before_rebuild = embeddings.calls
        rebuilt = service.scan("team-a/one.md", force=True)
        self.assertEqual(rebuilt["indexed"], 1)
        self.assertEqual(embeddings.calls, calls_before_rebuild)
        first.write_text("# Orchard\nupdated harvest", encoding="utf-8")
        self.assertEqual(service.scan("team-a/one.md")["indexed"], 1)
        self.assertTrue(service.search("harvest"))
        second.unlink()
        self.assertEqual(service.scan()["removed"], 1)
        self.assertEqual(service.status()["documents"], 1)

    def test_watcher_create_modify_move_delete(self):
        settings = Settings(
            root=self.root,
            home=self.home,
            chunk_chars=100,
            chunk_overlap=10,
            reconcile_seconds=0.2,
        )
        service = LocalRAG(settings)
        watcher = WatchService(service)
        import threading

        thread = threading.Thread(target=watcher.run, daemon=True)
        thread.start()
        try:
            note = self.root / "watched.txt"
            note.write_text("created token", encoding="utf-8")
            self.assertTrue(_wait(lambda: bool(service.search("created"))))
            note.write_text("modified token", encoding="utf-8")
            self.assertTrue(_wait(lambda: bool(service.search("modified"))))
            moved = self.root / "renamed.txt"
            note.rename(moved)
            self.assertTrue(_wait(lambda: service.search("modified")[0]["path"] == "renamed.txt"))
            moved.unlink()
            self.assertTrue(_wait(lambda: service.status()["documents"] == 0))
        finally:
            watcher.stop()
            thread.join(3)

    def test_mcp_read_and_admin_workflows(self):
        (self.root / "note.txt").write_text("MCP searchable content", encoding="utf-8")
        service = LocalRAG(self.settings)
        service.scan()
        server = MCPServer(service)
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
        self.assertIn("local_rag_read", names)
        self.assertIn("local_rag_admin_reindex", names)
        called = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "local_rag_search", "arguments": {"query": "searchable"}},
            }
        )
        payload = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(payload[0]["path"], "note.txt")
        failed = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "local_rag_admin_metadata_add",
                    "arguments": {"path": "note.txt", "key": "x", "value": 1, "evidence": []},
                },
            }
        )
        self.assertTrue(failed["result"]["isError"])

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


def _wait(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except (IndexError, ValueError):
            pass
        time.sleep(0.05)
    return False


if __name__ == "__main__":
    unittest.main()
