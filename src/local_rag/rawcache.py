"""Raw Drive file cache lookup shared by page images and the integrity check."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_KEY = re.compile(r"[0-9a-f]{64}\.[a-z0-9]{1,8}")


def raw_cache_file(cache_dir: Path, document: Any) -> Path | None:
    """The cached raw Drive download for ``document``, or ``None`` when it is not on disk."""
    key = json.loads(document["metadata_json"]).get("drive_raw_key")
    if not isinstance(key, str) or not _KEY.fullmatch(key):  # never follow metadata as a path
        return None
    path = cache_dir / "sources" / str(document["source_id"]) / "raw" / key
    return path if path.is_file() else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def raw_cache_matches(cache_dir: Path, document: Any) -> bool | None:
    """Whether the cached raw file hashes to the indexed ``content_hash`` (``None``: no cache).

    Drive indexes ``sha256`` of exactly this file, so the comparison is like with like.
    """
    path = raw_cache_file(cache_dir, document)
    return None if path is None else file_sha256(path) == document["content_hash"]
