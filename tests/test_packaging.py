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
        self.assertEqual(project["version"], "0.11.0")
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
        drive_dependencies = project["optional-dependencies"]["google-drive"]
        for package in ("google-api-python-client", "google-auth-oauthlib"):
            matches = [item for item in drive_dependencies if item.startswith(f"{package}==")]
            self.assertEqual(len(matches), 1, f"{package} must be pinned in the google-drive extra")
            self.assertFalse(
                any(item.startswith(package) for item in project["dependencies"]),
                f"{package} must not be installed by the base distribution",
            )
        extras = project["optional-dependencies"]
        self.assertEqual(extras["rerank"], extras["local-embeddings"])
        self.assertTrue(any(item.startswith("markitdown") for item in extras["markitdown"]))
        self.assertIn("Copyright 2026 phamviet86", (ROOT / "LICENSE").read_text())
        manifest = (ROOT / "MANIFEST.in").read_text()
        for included_file in ("SECURITY.md", "AGENTS.md", "CONTRIBUTING.md", ".env.example"):
            self.assertIn(included_file, manifest)


class LegacyEntrypointTests(unittest.TestCase):
    def test_local_rag_alias_warns_on_stderr_and_still_runs(self) -> None:
        import contextlib
        import io

        from local_rag.cli import legacy_entrypoint

        err = io.StringIO()
        with (
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            legacy_entrypoint(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("'local-rag' is deprecated", err.getvalue())
        self.assertIn("next minor release", err.getvalue())


if __name__ == "__main__":
    unittest.main()
