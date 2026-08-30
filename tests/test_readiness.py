from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_rag.cli import entrypoint, main, parser
from local_rag.config import Settings
from local_rag.embeddings import UnavailableEmbeddings
from local_rag.mcp import MCPServer
from local_rag.ocr_runtime import OCR_MODEL_FILES, OCRRuntimeManager
from local_rag.service import MultiSourceRAG
from local_rag.service_manager import AutoIndexService


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.home = base / "home"
        self.root = base / "documents"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self, argv: list[str], *, wrapped: bool = False) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = entrypoint(argv) if wrapped else main(argv)
        return code, json.loads(output.getvalue())

    def test_setup_modes_are_explicit_and_no_ocr_is_supported(self) -> None:
        with self.assertRaises(SystemExit):
            parser().parse_args(["setup"])
        code, result = self.capture(["--home", str(self.home), "setup", "--no-ocr"])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(Settings.load(self.home).ocr_mode, "no-ocr")
        self.assertEqual(Settings.load(self.home).reconcile_seconds, 600)
        self.assertIn("service install", result["service_recommendation"])
        self.assertEqual(parser().parse_args(["service", "status"]).service_command, "status")

        doctor_code, doctor = self.capture(["--home", str(self.home), "doctor", "--json"])
        self.assertEqual(doctor_code, 2)
        self.assertEqual(doctor["status"], "blocked")
        self.assertEqual(doctor["checks"]["sources"]["status"], "error")
        self.assertFalse(doctor["capabilities"]["semantic"])
        self.assertEqual(doctor["checks"]["ocr"]["status"], "warning")

    def test_full_setup_provisions_and_persists_mode(self) -> None:
        manifest = {"verified": "true", "ocr_model_revision": "test"}
        with patch.object(OCRRuntimeManager, "provision_and_verify", return_value=manifest):
            code, result = self.capture(["--home", str(self.home), "setup", "--full"])
        self.assertEqual(code, 0)
        self.assertEqual(result["ocr"], manifest)
        self.assertEqual(Settings.load(self.home).ocr_mode, "full")

    def test_macos_service_start_surfaces_bootstrap_failure(self) -> None:
        manager = AutoIndexService(
            self.home, user_home=self.home, system="darwin", executable="/bin/true"
        )
        manager.install()
        inactive = types.SimpleNamespace(returncode=113, stdout="", stderr="not loaded")
        failed = types.SimpleNamespace(
            returncode=5,
            stdout="",
            stderr=f"Bootstrap failed for {self.home}: 5: Input/output error",
        )
        with (
            patch("local_rag.service_manager.subprocess.run", side_effect=[inactive, failed]),
            self.assertRaisesRegex(RuntimeError, r"bootstrap failed \(exit 5\)") as raised,
        ):
            manager.start()
        message = str(raised.exception)
        self.assertIn("plutil -lint", message)
        self.assertIn("launchd unified log", message)
        self.assertNotIn(str(self.home), message)

    def test_macos_service_start_skips_bootstrap_only_when_already_active(self) -> None:
        manager = AutoIndexService(
            self.home, user_home=self.home, system="darwin", executable="/bin/true"
        )
        manager.install()
        active = types.SimpleNamespace(returncode=0, stdout="active", stderr="")
        with patch("local_rag.service_manager.subprocess.run", return_value=active) as run:
            result = manager.start()
        self.assertTrue(result["active"])
        run.assert_called_once()

    def test_ocr_provision_warms_then_verifies_offline_cache(self) -> None:
        manager = OCRRuntimeManager(self.home / "runtime", self.home / "models")
        manager.runtime_dir.mkdir(parents=True)
        calls: list[dict[str, object]] = []

        def process(path: str, **values: object) -> object:
            self.assertTrue(Path(path).exists())
            calls.append(values)
            if values["offline"] is False:
                revision = manager.model_dir / "pp-ocrv6-small" / "revision"
                revision.mkdir(parents=True)
                for name in OCR_MODEL_FILES:
                    (revision / name).write_bytes(b"fixture")
            return types.SimpleNamespace(pages=[object()])

        fake = types.SimpleNamespace(process_pdf_with_ocr=process)
        with (
            patch.object(manager, "install", return_value={"ocr_model_revision": "pinned"}),
            patch.object(manager, "configure", return_value=True),
            patch.dict(sys.modules, {"pdf_inspector": fake}),
        ):
            result = manager.provision_and_verify()
        self.assertEqual([call["offline"] for call in calls], [False, True])
        self.assertNotIn("model_directory", calls[0])
        self.assertTrue(str(calls[1]["model_directory"]).endswith("revision"))
        self.assertEqual(result["verified"], "true")

    def test_ocr_resolves_revision_nested_pdf_inspector_model_cache(self) -> None:
        manager = OCRRuntimeManager(self.home / "runtime", self.home / "models")
        revision = manager.model_dir / "pp-ocrv6-small" / "oar-ocr-v0.7.0"
        revision.mkdir(parents=True)
        for name in OCR_MODEL_FILES:
            (revision / name).write_bytes(b"fixture")

        self.assertEqual(manager.resolved_model_dir(), revision.resolve())

    def test_zero_source_contract_then_degraded_fts_indexing(self) -> None:
        settings = Settings(root=self.home, home=self.home, ocr_mode="no-ocr")
        settings.save()
        service = MultiSourceRAG(settings)
        self.assertEqual(service.source_summary()["error"]["code"], "no_sources")
        self.assertEqual(service.search("anything")["error"]["code"], "no_enabled_sources")
        self.assertEqual(service.reconcile()["error"]["code"], "no_enabled_sources")

        (self.root / "note.txt").write_text("searchable local marker", encoding="utf-8")
        service.add_local_source("notes", self.root)
        report = service.reconcile()
        self.assertFalse(report["errors"])
        self.assertIn("vectors are unavailable", report["sources"][0]["warnings"][0])
        self.assertTrue(service.search("marker", mode="full_text")["results"])
        doctor = service.doctor()
        self.assertTrue(doctor["ok"])
        self.assertEqual(doctor["status"], "degraded")
        self.assertTrue(doctor["capabilities"]["full_text"])
        self.assertFalse(doctor["capabilities"]["semantic"])

    def test_mcp_initialization_and_zero_source_guidance(self) -> None:
        settings = Settings(root=self.home, home=self.home, ocr_mode="no-ocr")
        settings.save()
        server = MCPServer(MultiSourceRAG(settings), "reader")
        initialized = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        self.assertIn("never request credential contents", initialized["result"]["instructions"])
        self.assertIn("doctor", server.allowed)
        self.assertEqual(server.call("sources", {})["error"]["code"], "no_sources")
        self.assertEqual(
            server.call("search", {"query": "x"})["error"]["code"], "no_enabled_sources"
        )

    def test_console_entrypoint_returns_stable_json_error(self) -> None:
        code, result = self.capture(["--home", str(self.home), "status"], wrapped=True)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "filenotfound")
        self.assertIn("setup --full", result["error"]["message"])
        self.assertIn("setup --no-ocr", result["error"]["message"])
        self.assertNotIn("traceback", json.dumps(result).lower())

    def test_doctor_rejects_unusable_embedding_configurations(self) -> None:
        base = Settings(root=self.home, home=self.home, ocr_mode="no-ocr")
        base.save()
        initial = MultiSourceRAG(base)
        initial.add_local_source("docs", self.root)

        unavailable = UnavailableEmbeddings("local", "missing", "provider unavailable")
        injected = MultiSourceRAG(base, embeddings=unavailable).doctor()["checks"]["embeddings"]
        self.assertFalse(injected["available"])
        self.assertEqual(injected["message"], "provider unavailable")

        remote = Settings(
            root=self.home,
            home=self.home,
            ocr_mode="no-ocr",
            embedding_provider="openai",
            embedding_model="provider/model",
        )
        missing_key = MultiSourceRAG(remote).doctor()["checks"]["embeddings"]
        self.assertFalse(missing_key["available"])
        self.assertIn("OPENAI_API_KEY", missing_key["message"])

        missing_model_settings = Settings(
            root=self.home,
            home=self.home,
            ocr_mode="no-ocr",
            embedding_provider="local",
        )
        missing_model = MultiSourceRAG(missing_model_settings).doctor()["checks"]["embeddings"]
        self.assertFalse(missing_model["available"])
        self.assertIn("EMBEDDING_MODEL", missing_model["message"])

        local = Settings(
            root=self.home,
            home=self.home,
            ocr_mode="no-ocr",
            embedding_provider="local",
            embedding_model="local/model",
        )
        with patch("local_rag.embeddings.find_spec", return_value=None):
            missing_dependency = MultiSourceRAG(local).doctor()["checks"]["embeddings"]
        self.assertFalse(missing_dependency["available"])
        self.assertIn("dependency", missing_dependency["message"])


if __name__ == "__main__":
    unittest.main()
