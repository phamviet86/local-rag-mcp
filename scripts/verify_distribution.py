#!/usr/bin/env python3
"""Check that built release artifacts contain the files users need to deploy."""

from __future__ import annotations

import argparse
import email
import tarfile
import zipfile
from pathlib import Path

REQUIRED_SDIST_FILES = {
    ".env.example",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "README.vi.md",
    "SECURITY.md",
    "config/mcp.json.example",
    "docs/agents.md",
    "docs/deployment.md",
    "docs/reference.md",
    "docs/setup.md",
    "pyproject.toml",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--distribution", default="phamviet-local-rag-mcp")
    parser.add_argument("--version", default="0.8.0")
    return parser


def _sdist_files(path: Path, root: str) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        prefix = f"{root}/"
        return {
            name.removeprefix(prefix)
            for member in archive.getmembers()
            if member.isfile() and (name := member.name).startswith(prefix)
        }


def main() -> int:
    args = _parser().parse_args()
    normalized = args.distribution.replace("-", "_")
    stem = f"{normalized}-{args.version}"
    wheel = args.dist_dir / f"{stem}-py3-none-any.whl"
    sdist = args.dist_dir / f"{stem}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        raise SystemExit(f"expected one wheel and one sdist named {stem} in {args.dist_dir}")

    missing = REQUIRED_SDIST_FILES - _sdist_files(sdist, stem)
    if missing:
        raise SystemExit(f"sdist is missing release files: {sorted(missing)}")

    with zipfile.ZipFile(wheel) as archive:
        metadata_path = f"{stem}.dist-info/METADATA"
        entry_points_path = f"{stem}.dist-info/entry_points.txt"
        license_path = f"{stem}.dist-info/licenses/LICENSE"
        names = set(archive.namelist())
        required_wheel = {metadata_path, entry_points_path, license_path}
        if missing := required_wheel - names:
            raise SystemExit(f"wheel is missing required metadata: {sorted(missing)}")
        metadata = email.message_from_bytes(archive.read(metadata_path))
        if metadata["Name"] != args.distribution or metadata["Version"] != args.version:
            raise SystemExit(
                "wheel metadata does not match release target: "
                f"{metadata['Name']} {metadata['Version']}"
            )
        entry_points = archive.read(entry_points_path).decode()
        if "local-rag-mcp = local_rag.cli:entrypoint" not in entry_points:
            raise SystemExit("wheel does not expose the local-rag-mcp console script")

    print(f"Distribution contents verified: {wheel.name}, {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
