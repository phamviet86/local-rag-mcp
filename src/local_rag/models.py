from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceSpan:
    kind: str
    locator: str
    start: int
    end: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    spans: list[SourceSpan]
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    reviews: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkRecord:
    ordinal: int
    text: str
    start: int
    end: int
    chunk_hash: str
    provenance: list[dict[str, Any]]
    cite_key: str | None = None
    section_path: tuple[str, ...] = ()
    page_start: int | None = None
    page_end: int | None = None
    ocr: bool = False
    body_hash: str = ""  # hash of the body without context prefix; empty means chunk_hash
