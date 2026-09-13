import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_rag.ocr_runtime as runtime_module
from local_rag.config import DEFAULT_EXCLUSIONS, Settings
from local_rag.ocr_runtime import OCRRuntimeManager


class ConfigRuntimeTests(unittest.TestCase):
    def test_persistent_embedding_file_precedence_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            Settings(root=home, home=home).save()
            credentials = home / "credentials"
            credentials.mkdir(mode=0o700)
            path = credentials / "embeddings.json"
            path.write_text(
                json.dumps(
                    {
                        "embedding_provider": "openai",
                        "embedding_model": "fixture-model",
                        "openai_api_key": "fixture-secret-never-print",
                    }
                )
            )
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                loaded = Settings.load(home)
                self.assertEqual(loaded.openai_api_key, "fixture-secret-never-print")
                self.assertEqual(loaded.embedding_model, "fixture-model")
                loaded.save()
            self.assertNotIn("fixture-secret-never-print", (home / "config.json").read_text())
            with patch.dict(os.environ, {"LOCAL_RAG_MCP_OPENAI_API_KEY": "environment-secret"}):
                self.assertEqual(Settings.load(home).openai_api_key, "environment-secret")
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("LOCAL_RAG_", "LOCAL_RAG_MCP_"))
            }
            probe = subprocess.run(
                [sys.executable, "-m", "local_rag.cli", "--home", str(home), "doctor", "--json"],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(probe.returncode, 2)  # The fixture intentionally has no source.
            self.assertTrue(json.loads(probe.stdout)["checks"]["embeddings"]["available"])
            self.assertNotIn("fixture-secret-never-print", probe.stdout + probe.stderr)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                Settings.load(home)
            path.chmod(0o600)
            path.write_text('{"openai_api_key": "fixture-secret-never-print", BROKEN')
            with self.assertRaises(ValueError) as error:
                Settings.load(home)
            self.assertNotIn("fixture-secret-never-print", str(error.exception))
            path.unlink()
            target = home / "target.json"
            target.write_text("{}")
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                Settings.load(home)

    def test_one_root_layout_exclusions_and_symlink_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, home, outside = base / "root", base / "state", base / "outside.txt"
            root.mkdir()
            outside.write_text("outside")
            link = root / "linked.txt"
            link.symlink_to(outside)
            settings = Settings(root=root, home=home)
            settings.save()
            loaded = Settings.load(home)
            self.assertEqual(loaded.root, root.resolve())
            self.assertTrue(DEFAULT_EXCLUSIONS.issubset(loaded.exclusions))
            self.assertTrue(loaded.excluded(root / ".git" / "config"))
            self.assertFalse(loaded.accepts(link))
            self.assertTrue(loaded.database.parent.exists())
            self.assertTrue(loaded.extracted_dir.is_dir())
            self.assertTrue(loaded.model_dir.is_dir())
            self.assertTrue(loaded.cache_dir.is_dir())
            self.assertTrue(loaded.runtime_dir.is_dir())

    def test_runtime_download_is_pinned_and_checksum_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archives = {}
            for name, library in (("pdfium", "libpdfium.so"), ("ort", "libonnxruntime.so")):
                payload = base / name
                payload.mkdir()
                (payload / library).write_bytes(name.encode())
                archive = base / f"{name}.tgz"
                with tarfile.open(archive, "w:gz") as handle:
                    handle.add(payload / library, arcname=library)
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                archives[name] = (archive.as_uri(), digest)
            manager = OCRRuntimeManager(base / "runtime", base / "models")
            with (
                patch.object(runtime_module, "ARTIFACTS", {("test", "arch"): archives}),
                patch.object(OCRRuntimeManager, "platform_key", return_value=("test", "arch")),
            ):
                manifest = manager.install()
            self.assertEqual(manifest["onnxruntime_version"], "1.27.0")
            self.assertTrue(manager.configure())
            saved = json.loads((base / "runtime" / "manifest.json").read_text())
            self.assertIn("oar-ocr-v0.7.0", saved["ocr_model_revision"])


if __name__ == "__main__":
    unittest.main()
