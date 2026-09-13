"""Install self-contained agent skills from the installed distribution."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from importlib.metadata import distribution
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__

SKILL_NAMES = ("local-rag-setup", "local-rag")
MANIFEST = "references/installation.json"
DISTRIBUTION = "phamviet-local-rag-mcp"


def default_skills_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    return (
        Path(codex_home).expanduser() / "skills" if codex_home else Path.home() / ".agents/skills"
    )


def _assets(root: Traversable, prefix: str = "") -> dict[str, bytes]:
    result = {}
    for item in root.iterdir():
        name = prefix + item.name
        if item.is_dir():
            result.update(_assets(item, name + "/"))
        elif item.is_file():
            result[name] = item.read_bytes()
    return result


def _runtime() -> bytes:
    # Keep the virtualenv path: resolving a symlinked Python would lose its scripts directory.
    python = Path(sys.executable).absolute()
    cli = python.parent / "local-rag-mcp"
    server = python.parent / "local-rag-mcp-server"
    if not cli.is_file() or not server.is_file():
        raise RuntimeError("install the distribution in this Python environment before its skills")
    source = None
    direct_url = distribution(DISTRIBUTION).read_text("direct_url.json")
    if direct_url:
        origin = json.loads(direct_url).get("url", "")
        parsed = urlsplit(origin)
        if (
            parsed.scheme in {"file", "https"}
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        ):
            source = origin
    return (
        json.dumps(
            {
                "distribution": DISTRIBUTION,
                "version": __version__,
                "python": str(python),
                "cli": str(cli),
                "mcp_server": str(server),
                "package_source": source,
            },
            indent=2,
        )
        + "\n"
    ).encode()


def _owned(target: Path) -> set[str] | None:
    try:
        manifest = json.loads((target / MANIFEST).read_text())
        paths = manifest["files"]
        if manifest.get("distribution") != DISTRIBUTION or not isinstance(paths, list):
            return None
        if not all(
            isinstance(path, str) and not Path(path).is_absolute() and ".." not in Path(path).parts
            for path in paths
        ):
            return None
        return set(paths) | {MANIFEST}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _state(target: Path, expected: dict[str, bytes]) -> str:
    if target.is_symlink():
        return "unsafe"
    if not target.exists():
        return "missing"
    if not target.is_dir():
        return "unmanaged"
    actual = {}
    for path in target.rglob("*"):
        if path.is_symlink():
            return "unsafe"
        if path.is_file():
            actual[path.relative_to(target).as_posix()] = path.read_bytes()
    owned = _owned(target)
    if owned is None:
        return "unmanaged"
    managed = {key: value for key, value in actual.items() if key in owned or key in expected}
    return "unchanged" if managed == expected else "conflict"


def install_skills(
    dest: Path | None = None,
    *,
    check: bool = False,
    dry_run: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    destination = (dest if dest is not None else default_skills_root()).expanduser().resolve()
    runtime = _runtime()
    bundles = {}
    results = []
    for name in SKILL_NAMES:
        bundle = _assets(files("local_rag").joinpath("skills", name))
        bundle["references/runtime.json"] = runtime
        bundle[MANIFEST] = (
            json.dumps({"distribution": DISTRIBUTION, "files": sorted(bundle)}, indent=2) + "\n"
        ).encode()
        bundles[name] = bundle
        target = destination / name
        results.append({"name": name, "path": str(target), "status": _state(target, bundle)})
    blocked = any(
        item["status"] in {"unsafe", "unmanaged"} or (item["status"] == "conflict" and not replace)
        for item in results
    )
    ok = all(item["status"] == "unchanged" for item in results) if check else not blocked
    report: dict[str, Any] = {
        "ok": ok,
        "mode": "check" if check else ("dry-run" if dry_run else "install"),
        "destination": str(destination),
        "skills": results,
    }
    if blocked:
        report["message"] = (
            "No skills changed. Different managed skills require --replace; "
            "unmanaged or symlinked skill folders are never replaced. Choose another --dest."
        )
    if check or dry_run or blocked or all(item["status"] == "unchanged" for item in results):
        return report

    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".local-rag-skills-", dir=destination) as temporary:
        staging = Path(temporary)
        changed = []
        try:
            for item in results:
                if item["status"] == "unchanged":
                    continue
                name = item["name"]
                staged = staging / name
                target = destination / name
                if target.exists():
                    owned = _owned(target)
                    assert owned is not None
                    for existing in target.rglob("*"):
                        relative = existing.relative_to(target).as_posix()
                        if (
                            existing.is_file()
                            and relative not in owned
                            and relative not in bundles[name]
                        ):
                            extra = staged / relative
                            extra.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(existing, extra)
                for relative, contents in bundles[name].items():
                    path = staged / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(contents)
                backup = staging / f"{name}.previous"
                # Recheck before replacing so an intervening edit is not silently overwritten.
                if _state(target, bundles[name]) != item["status"]:
                    raise RuntimeError(f"skill changed during installation: {target}")
                if target.exists():
                    target.rename(backup)
                changed.append((target, backup))
                staged.rename(target)
            for item in results:
                if item["status"] != "unchanged":
                    item["status"] = "replaced" if item["status"] == "conflict" else "installed"
        except Exception:
            for target, backup in reversed(changed):
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                if backup.exists():
                    backup.rename(target)
            raise
    return report
