from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_rag.skill_install import SKILL_NAMES, default_skills_root, install_skills


class SkillInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.dest = self.base / "parent with spaces" / "skills"

    def test_default_scope_honors_codex_home_then_agents(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": str(self.base)}):
            self.assertEqual(default_skills_root(), self.base / "skills")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("pathlib.Path.home", return_value=self.base),
        ):
            self.assertEqual(default_skills_root(), self.base / ".agents/skills")

    def test_preview_and_check_do_not_create_parents_or_runtime_data(self) -> None:
        for option, code in (("--dry-run", 0), ("--check", 2)):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "local_rag.cli",
                    "--home",
                    str(self.base / "data"),
                    "install-skills",
                    "--dest",
                    str(self.dest),
                    option,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)["skills"]), 2)
            self.assertFalse(self.dest.parent.exists())
            self.assertFalse((self.base / "data").exists())

    def test_install_is_idempotent_and_runtime_does_not_require_path(self) -> None:
        with patch.dict(os.environ, {"LOCAL_RAG_MCP_OPENAI_API_KEY": "sentinel-do-not-export"}):
            result = install_skills(self.dest)
        self.assertTrue(result["ok"])
        before = {p: p.stat().st_mtime_ns for p in self.dest.rglob("*") if p.is_file()}
        self.assertTrue(install_skills(self.dest, check=True)["ok"])
        self.assertTrue(
            all(x["status"] == "unchanged" for x in install_skills(self.dest)["skills"])
        )
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        for name in SKILL_NAMES:
            runtime_file = self.dest / name / "references/runtime.json"
            runtime = json.loads(runtime_file.read_text())
            self.assertNotIn("sentinel-do-not-export", runtime_file.read_text())
            self.assertTrue(Path(runtime["cli"]).is_absolute())
            command = subprocess.run(
                [runtime["cli"], "--version"],
                cwd=self.base,
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("local-rag-mcp", command.stdout)

    def test_conflict_blocks_both_and_replace_preserves_unrelated_content(self) -> None:
        install_skills(self.dest)
        first = self.dest / SKILL_NAMES[0] / "SKILL.md"
        first.write_text("operator modification")
        extra = self.dest / SKILL_NAMES[0] / "notes.txt"
        extra.write_text("keep my notes")
        unrelated = self.dest / "other-skill"
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text("keep other skill")
        second = self.dest / SKILL_NAMES[1] / "SKILL.md"
        second.unlink()
        self.assertFalse(install_skills(self.dest)["ok"])
        self.assertFalse(install_skills(self.dest, check=True, replace=True)["ok"])
        self.assertTrue(install_skills(self.dest, dry_run=True, replace=True)["ok"])
        self.assertFalse(second.exists())
        self.assertEqual(first.read_text(), "operator modification")
        self.assertTrue(install_skills(self.dest, replace=True)["ok"])
        self.assertTrue(install_skills(self.dest, check=True)["ok"])
        self.assertEqual(extra.read_text(), "keep my notes")
        self.assertEqual((unrelated / "SKILL.md").read_text(), "keep other skill")

    def test_unmanaged_and_symlinked_collisions_are_never_replaced(self) -> None:
        first = self.dest / SKILL_NAMES[0]
        first.mkdir(parents=True)
        (first / "SKILL.md").write_text("another project")
        self.assertFalse(install_skills(self.dest, replace=True)["ok"])
        self.assertFalse((self.dest / SKILL_NAMES[1]).exists())
        (first / "SKILL.md").unlink()
        first.rmdir()
        outside = self.base / "outside"
        outside.mkdir()
        first.symlink_to(outside, target_is_directory=True)
        self.assertFalse(install_skills(self.dest, replace=True)["ok"])
        self.assertEqual(list(outside.iterdir()), [])
        first.unlink()
        install_skills(self.dest)
        (first / "external").symlink_to(outside, target_is_directory=True)
        self.assertFalse(install_skills(self.dest, replace=True)["ok"])
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
