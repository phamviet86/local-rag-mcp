from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_rag.config import Settings
from local_rag.mcp import MCPServer
from local_rag.service import MultiSourceRAG
from local_rag.service_manager import AutoIndexService


class JobAndServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.home, self.root = base / "home", base / "source"
        self.root.mkdir()
        self.settings = Settings(root=self.home, home=self.home, ocr_mode="no-ocr")
        self.settings.save()
        self.service = MultiSourceRAG(self.settings)
        self.service.add_local_source("docs", self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_single_writer_coalescing_progress_and_search_during_index(self) -> None:
        (self.root / "committed.txt").write_text("committed searchable marker", encoding="utf-8")
        self.service.reconcile("docs")
        entered, release = threading.Event(), threading.Event()
        original = self.service.reconcile

        def slow(*args: object, **kwargs: object) -> dict[str, object]:
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with patch.object(self.service, "reconcile", side_effect=slow):
            first = self.service.start_index_job(
                "reindex", "docs", "committed.txt", background=True
            )
            self.assertTrue(entered.wait(5))
            active = self.service.index_status()
            self.assertTrue(active["active"])
            self.assertNotIn("target", active["job"])
            self.assertNotIn("committed.txt", str(active))
            self.assertTrue(self.service.search("searchable", mode="full_text")["results"])
            same = self.service.enqueue_index_job("reindex", "docs", "committed.txt")
            self.assertEqual(same["id"], first["id"])
            self.assertTrue(same["coalesced"])
            rejected = self.service.enqueue_index_job("reconcile", "docs")
            self.assertEqual(rejected["state"], "rejected")
            release.set()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            completed = self.service.job_status(str(first["id"]), reader=False)
            if completed["state"] == "complete":
                break
            time.sleep(0.02)
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(completed["remaining"], 0)
        self.assertGreaterEqual(completed["searchable"], 1)
        self.assertFalse(self.service.index_status()["active"])

    def test_manual_job_targets_and_error_state(self) -> None:
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "one.txt").write_text("first target marker", encoding="utf-8")
        (self.root / "two.txt").write_text("second target marker", encoding="utf-8")
        global_job = self.service.start_index_job("reconcile", background=False)
        self.assertEqual(global_job["state"], "complete")
        source_job = self.service.start_index_job("reindex", "docs", background=False)
        self.assertEqual(source_job["state"], "complete")
        folder_job = self.service.start_index_job("reindex", "docs", "folder", background=False)
        self.assertEqual(folder_job["state"], "complete")
        file_job = self.service.start_index_job(
            "reindex", "docs", "two.txt", reextract=True, background=False
        )
        self.assertEqual(file_job["state"], "complete")
        failed = self.service.start_index_job("reindex", "docs", "missing.txt", background=False)
        self.assertEqual(failed["state"], "complete")
        self.assertEqual(len(self.service.list_jobs()), 5)

    def test_new_files_report_live_intermediate_progress(self) -> None:
        for index in range(3):
            (self.root / f"new-{index}.txt").write_text(
                f"new progress marker {index}", encoding="utf-8"
            )
        entered, release = threading.Event(), threading.Event()
        original = self.service.extractor.extract
        calls = 0

        def slow(path: Path):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                self.assertTrue(release.wait(5))
            return original(path)

        with patch.object(self.service.extractor, "extract", side_effect=slow):
            job = self.service.start_index_job("reconcile", "docs", background=True)
            self.assertTrue(entered.wait(5))
            progress = self.service.job_status(str(job["id"]), reader=True)
            self.assertEqual(progress["discovered"], 3)
            self.assertEqual(progress["processed"], 1)
            self.assertEqual(progress["remaining"], 2)
            self.assertEqual(progress["searchable"], 0)
            release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            completed = self.service.job_status(str(job["id"]), reader=True)
            if completed["state"] == "complete":
                break
            time.sleep(0.02)
        self.assertEqual(completed["processed"], 3)
        self.assertEqual(completed["searchable"], 3)
        self.assertEqual(completed["remaining"], 0)

    def test_mcp_background_job_lives_for_reader_polling(self) -> None:
        (self.root / "mcp.txt").write_text("MCP background progress", encoding="utf-8")
        admin, reader = MCPServer(self.service, "admin"), MCPServer(self.service, "reader")
        job = admin.call("start_reconcile", {"source": "docs"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = reader.call("job_status", {"job_id": job["id"]})
            if status["state"] == "complete":
                break
            time.sleep(0.02)
        self.assertEqual(status["state"], "complete")
        self.assertEqual(status["searchable"], 1)

    def test_platform_units_are_optional_and_contain_no_secrets(self) -> None:
        base = Path(self.temporary.name) / "home with spaces"
        data_home = base / "data with spaces"
        for system in ("darwin", "linux"):
            user_home = base / system
            manager = AutoIndexService(
                data_home,
                user_home=user_home,
                system=system,
                executable="/opt/local-rag-mcp/bin/local-rag-mcp",
            )
            with patch.object(manager, "_run"):
                installed = manager.install()
            self.assertTrue(installed["installed"])
            content = manager.unit_path.read_bytes().decode("utf-8", errors="ignore")
            self.assertIn("local-rag-mcp", content)
            self.assertIn(str(manager.data_home), content)
            if system == "linux":
                self.assertIn(f'--home "{manager.data_home}"', content)
            self.assertNotIn("API_KEY", content)
            self.assertNotIn("token", content.lower())
            with (
                patch.object(manager, "_run") as run,
                patch.object(
                    manager,
                    "status",
                    return_value={"installed": True, "active": True},
                ),
            ):
                self.assertTrue(manager.start()["active"])
                manager.stop()
                self.assertTrue(run.called)
            with patch.object(manager, "stop"), patch.object(manager, "_run"):
                removed = manager.uninstall()
            self.assertFalse(removed["installed"])
            self.assertFalse(manager.unit_path.exists())


if __name__ == "__main__":
    unittest.main()
