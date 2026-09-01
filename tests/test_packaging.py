from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

import local_rag
import local_rag_mcp

ROOT = Path(__file__).resolve().parents[1]


class PackagingMetadataTests(unittest.TestCase):
    def test_distribution_identity_matches_runtime_version(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]

        self.assertEqual(project["name"], "phamviet-local-rag-mcp")
        self.assertEqual(project["version"], local_rag.__version__)
        self.assertEqual(local_rag_mcp.__version__, local_rag.__version__)
        self.assertEqual(
            project["scripts"],
            {
                "local-rag-mcp": "local_rag.cli:entrypoint",
                "local-rag-mcp-server": "local_rag.mcp:main",
                "local-rag": "local_rag.cli:legacy_entrypoint",
            },
        )

    def test_optional_integrations_and_release_files_are_declared(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]

        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(
            project["optional-dependencies"]["google-drive"],
            ["google-api-python-client==2.198.0", "google-auth-oauthlib==1.4.0"],
        )
        self.assertNotIn("google-api-python-client==2.198.0", project["dependencies"])
        self.assertNotIn("google-auth-oauthlib==1.4.0", project["dependencies"])
        self.assertIn("Copyright 2026 phamviet86", (ROOT / "LICENSE").read_text())
        manifest = (ROOT / "MANIFEST.in").read_text()
        for included_file in ("SECURITY.md", "AGENTS.md", ".env.example"):
            self.assertIn(included_file, manifest)


if __name__ == "__main__":
    unittest.main()
