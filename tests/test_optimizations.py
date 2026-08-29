import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_rag.config import Settings
from local_rag.db import MIGRATIONS, Database
from local_rag.indexer import _file_hash
from local_rag.search import SemanticSearchUnavailable
from local_rag.service import LocalRAG
from tests.helpers import write_text_pdf


class BatchEmbeddings:
    identity = ("test", "batch-v1")

    def __init__(self):
        self.calls = 0
        self.batch_sizes = []

    def embed(self, texts):
        self.calls += 1
        self.batch_sizes.append(len(texts))
        return [
            [float("orchard" in text.lower()), float("ocean" in text.lower()), 1.0]
            for text in texts
        ]


class FailingEmbeddings:
    identity = ("failing", "v1")

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        raise RuntimeError("provider offline")


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root, self.home = base / "root", base / "home"
        self.root.mkdir()
        self.settings = Settings(root=self.root, home=self.home, chunk_chars=100, chunk_overlap=10)

    def tearDown(self):
        self.temp.cleanup()

    def test_unchanged_files_are_not_hashed_and_stat_refresh_is_persistent(self):
        note = self.root / "note.txt"
        note.write_text("stable content", encoding="utf-8")
        service = LocalRAG(self.settings)
        service.scan()
        with patch("local_rag.indexer._file_hash", wraps=_file_hash) as hashed:
            unchanged = service.scan()
            self.assertEqual(hashed.call_count, 0)
            self.assertEqual(unchanged["unchanged"], 1)
            stat = note.stat()
            os.utime(note, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
            refreshed = service.scan()
            self.assertEqual(hashed.call_count, 1)
            self.assertEqual(refreshed["stat_refreshed"], 1)
            hashed.reset_mock()
            service.scan()
            self.assertEqual(hashed.call_count, 0)

    def test_reindex_uses_cached_artifact_and_reextract_is_explicit(self):
        note = self.root / "note.txt"
        note.write_text("cached extraction content", encoding="utf-8")
        service = LocalRAG(self.settings)
        service.scan()

        with (
            patch("local_rag.indexer._file_hash", wraps=_file_hash) as hashed,
            patch.object(
                service.extractor, "extract", side_effect=AssertionError("extractor reran")
            ) as extracted,
        ):
            rebuilt = service.scan("note.txt", force_index=True)
        self.assertEqual(rebuilt["indexed"], 1)
        hashed.assert_called_once_with(note.resolve())
        extracted.assert_not_called()
        self.assertIn("cached extraction content", service.read("note.txt")["text"])

        with patch.object(
            service.extractor, "extract", wraps=service.extractor.extract
        ) as extracted:
            rebuilt = service.scan("note.txt", force_index=True, reextract=True)
        self.assertEqual(rebuilt["indexed"], 1)
        extracted.assert_called_once_with(note.resolve())

    def test_reindex_hashes_equal_size_file_with_restored_mtime(self):
        note = self.root / "note.txt"
        note.write_text("alpha marker", encoding="utf-8")
        service = LocalRAG(self.settings)
        service.scan()
        original = note.stat()

        note.write_text("bravo marker", encoding="utf-8")
        os.utime(note, ns=(original.st_atime_ns, original.st_mtime_ns))
        with patch.object(
            service.extractor, "extract", wraps=service.extractor.extract
        ) as extracted:
            rebuilt = service.scan("note.txt", force_index=True)

        self.assertEqual(rebuilt["indexed"], 1)
        extracted.assert_called_once_with(note.resolve())
        self.assertEqual(service.db.resolve_document("note.txt")["content_hash"], _file_hash(note))
        self.assertIn("bravo marker", service.read("note.txt")["text"])
        self.assertEqual(
            service.search("bravo", mode="full_text")["results"][0]["path"], "note.txt"
        )
        self.assertFalse(service.search("alpha", mode="full_text")["results"])

    def test_existing_schema_migrates_to_metadata_and_revision_indexes(self):
        database = Database(Path(self.temp.name) / "migration.sqlite3")
        with database.connect() as connection:
            connection.executescript(MIGRATIONS[0])
            connection.execute(
                """CREATE TABLE schema_migrations
                   (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
            )
            connection.execute("INSERT INTO schema_migrations(version) VALUES(1)")
            connection.execute(
                """CREATE TABLE jobs
                   (id TEXT PRIMARY KEY,kind TEXT,target TEXT,status TEXT,detail_json TEXT)"""
            )
        database.migrate()
        with database.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
        self.assertIn("metadata_fts", tables)
        self.assertIn("review_revisions", tables)
        self.assertNotIn("jobs", tables)

    def test_1000_files_use_bounded_batched_unique_embeddings(self):
        for index in range(1000):
            (self.root / f"{index:04}.txt").write_text(
                f"unique document {index} benchmark token", encoding="utf-8"
            )
        embeddings = BatchEmbeddings()
        service = LocalRAG(self.settings, embeddings)
        report = service.scan()
        self.assertEqual(report["indexed"], 1000)
        self.assertEqual(report["embedded"], 1000)
        self.assertLessEqual(embeddings.calls, 8)
        self.assertLessEqual(max(embeddings.batch_sizes), 128)
        calls = embeddings.calls
        service.scan()
        self.assertEqual(embeddings.calls, calls)

    def test_search_modes_query_cache_fallback_and_literal_scope(self):
        literal, similar = self.root / "100%_real", self.root / "100XXreal"
        literal.mkdir()
        similar.mkdir()
        (literal / "a.txt").write_text("orchard scoped token", encoding="utf-8")
        (similar / "b.txt").write_text("ocean scoped token", encoding="utf-8")
        embeddings = BatchEmbeddings()
        service = LocalRAG(self.settings, embeddings)
        service.scan()
        full_text = service.search("scoped", scope="100%_real", mode="full_text")
        self.assertEqual(full_text["effective_mode"], "full_text")
        self.assertEqual([item["path"] for item in full_text["results"]], ["100%_real/a.txt"])
        before = embeddings.calls
        first = service.search("orchard", mode="hybrid")
        second = service.search("orchard", mode="hybrid")
        self.assertEqual(first["effective_mode"], "hybrid")
        self.assertEqual(second["warnings"], [])
        self.assertEqual(embeddings.calls, before + 1)
        semantic = service.search("ocean", mode="semantic")
        self.assertEqual(semantic["effective_mode"], "semantic")
        self.assertTrue(semantic["results"][0]["provenance"])

        failing = LocalRAG(
            Settings(root=self.root, home=Path(self.temp.name) / "failed-home"),
            FailingEmbeddings(),
        )
        report = failing.scan()
        self.assertTrue(report["warnings"])
        hybrid = failing.search("orchard", mode="hybrid")
        self.assertEqual(hybrid["effective_mode"], "full_text")
        self.assertTrue(hybrid["warnings"])
        with self.assertRaises(SemanticSearchUnavailable):
            failing.search("orchard", mode="semantic")

        unconfigured = LocalRAG(
            Settings(
                root=self.root,
                home=Path(self.temp.name) / "unconfigured-home",
                embedding_provider="openai",
            )
        )
        report = unconfigured.scan()
        self.assertTrue(report["warnings"])
        self.assertTrue(unconfigured.search("orchard", mode="full_text")["results"])

    def test_metadata_relationships_are_searchable(self):
        first, second = self.root / "one.txt", self.root / "two.txt"
        first.write_text("first body", encoding="utf-8")
        second.write_text("second body", encoding="utf-8")
        service = LocalRAG(self.settings)
        service.scan()
        evidence = [{"path": "one.txt", "locator": "line:1", "quote": "first body"}]
        service.add_metadata("one.txt", "status", "zephyr-approved", evidence, "agent-a")
        service.add_relationship("one.txt", "two.txt", "supports-annex", evidence, "agent-a")
        metadata_hits = service.search("zephyr", mode="full_text")["results"]
        self.assertEqual(metadata_hits[0]["matched_via"], "agent_metadata")
        relationship_hits = service.search("supports", mode="full_text")["results"]
        self.assertTrue(any(hit["matched_via"] == "relationship" for hit in relationship_hits))

    def test_review_correction_rebuilds_search_without_reextracting_pdf(self):
        pdf = self.root / "document.pdf"
        write_text_pdf(pdf, "Original uncertain page")
        service = LocalRAG(self.settings)
        service.scan()
        review = service.reviews()[0]
        document = service.db.resolve_document("document.pdf")
        base_artifact = Path(document["artifact_path"])
        base_before = base_artifact.read_bytes()
        evidence = [{"path": "document.pdf", "locator": "page:1", "quote": "source scan"}]
        with patch.object(service.extractor, "extract", side_effect=AssertionError("OCR reran")):
            corrected = service.correct_review(
                review["id"], "Human corrected searchable phrase", evidence, "reviewer-a"
            )
        self.assertEqual(corrected["status"], "resolved")
        self.assertEqual(base_artifact.read_bytes(), base_before)
        read = service.read("document.pdf")
        self.assertIn("Human corrected searchable phrase", read["text"])
        self.assertTrue(read["has_review_corrections"])
        self.assertEqual(read["provenance"][0]["metadata"]["source"], "review_correction")
        hits = service.search("corrected", mode="full_text")["results"]
        self.assertEqual(hits[0]["path"], "document.pdf")
        self.assertEqual(service.status()["review_revisions"], 1)

        effective_before = service.db.resolve_document("document.pdf")["effective_artifact_path"]
        stat = pdf.stat()
        os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        with patch.object(service.extractor, "extract", side_effect=AssertionError("OCR reran")):
            rebuilt = service.scan("document.pdf", force_index=True)
        self.assertEqual(rebuilt["indexed"], 1)
        document_after = service.db.resolve_document("document.pdf")
        self.assertEqual(document_after["effective_artifact_path"], effective_before)
        self.assertEqual(service.db.review(review["id"])["status"], "resolved")
        self.assertEqual(service.status()["review_revisions"], 1)
        self.assertIn("Human corrected searchable phrase", service.read("document.pdf")["text"])


if __name__ == "__main__":
    unittest.main()
