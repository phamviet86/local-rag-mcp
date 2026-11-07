"""Text builder shared by every extractor: joins parts and records source spans."""

from __future__ import annotations

from typing import Any

from .models import SourceSpan


class Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.spans: list[SourceSpan] = []
        self.length = 0

    def add(
        self, value: str, kind: str, locator: str, metadata: dict[str, Any] | None = None
    ) -> None:
        cleaned = value.strip()
        if not cleaned:
            return
        separator = "\n\n" if self.parts else ""
        self.parts.append(separator + cleaned)
        start = self.length + len(separator)
        self.length += len(separator) + len(cleaned)
        self.spans.append(SourceSpan(kind, locator, start, self.length, metadata or {}))

    @property
    def text(self) -> str:
        return "".join(self.parts)
