import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_rag.ocr_runtime as runtime_module
from local_rag.config import DEFAULT_EXCLUSIONS, Settings
from local_rag.ocr_runtime import OCRRuntimeManager


class ConfigRuntimeTests(unittest.TestCase):
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
