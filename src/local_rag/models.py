from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class SourceSpan:
    kind: str
    locator: str
    start: int
    end: int
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    spans: List[SourceSpan]
    media_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    reviews: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkRecord:
    ordinal: int
    text: str
    start: int
    end: int
    chunk_hash: str
    provenance: List[Dict[str, Any]]
