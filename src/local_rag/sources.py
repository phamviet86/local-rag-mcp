from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database


@dataclass(frozen=True)
class SourceRecord:
    id: str
    name: str
    kind: str
    locator: str
    account: str
    enabled: bool
    config: dict[str, Any]
    cursor: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceRecord:
        return cls(
            str(value["id"]),
            str(value["name"]),
            str(value["kind"]),
            str(value["locator"]),
            str(value.get("account", "")),
            bool(value["enabled"]),
            dict(value.get("config", {})),
            value.get("cursor"),
        )


class SourceRegistry:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.db = database

    def add_local(self, name: str, root: Path, exclusions: list[str] | None = None) -> SourceRecord:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"local source root is not a directory: {resolved}")
        if resolved == self.settings.home or self.settings.home in resolved.parents:
            raise ValueError("application data root cannot be indexed as a source")
        for source in self.list():
            if source.kind == "local" and Path(source.locator).resolve() == resolved:
                raise ValueError(f"local root is already registered as source {source.name!r}")
        row = self.db.add_source(
            name,
            "local",
            str(resolved),
            config={"exclusions": exclusions or []},
        )
        return SourceRecord.from_dict(row)

    def add_drive(
        self,
        name: str,
        root_id: str,
        account: str,
        token_file: Path,
        shared_drive_id: str = "",
        exclusions: list[str] | None = None,
    ) -> SourceRecord:
        if not root_id.strip() or not account.strip():
            raise ValueError("Drive root ID and account label are required")
        token = token_file.expanduser().resolve()
        config = {
            "token_file": str(token),
            "shared_drive_id": shared_drive_id,
            "exclusions": exclusions or [],
        }
        row = self.db.add_source(name, "google_drive", root_id.strip(), account, config)
        return SourceRecord.from_dict(row)

    def list(self, enabled: bool | None = None) -> list[SourceRecord]:
        return [SourceRecord.from_dict(row) for row in self.db.sources(enabled)]

    def get(self, value: str) -> SourceRecord:
        return SourceRecord.from_dict(self.db.source(value))

    def enable(self, value: str, enabled: bool) -> SourceRecord:
        return SourceRecord.from_dict(self.db.set_source_enabled(value, enabled))

    def public(self, source: SourceRecord) -> dict[str, Any]:
        config = dict(source.config)
        token_configured = bool(config.pop("token_file", ""))
        return {
            "id": source.id,
            "name": source.name,
            "kind": source.kind,
            "locator": source.locator,
            "account": source.account,
            "enabled": source.enabled,
            "config": config,
            "token_configured": token_configured,
            "cursor_configured": bool(source.cursor),
        }


def source_settings(base: Settings, source: SourceRecord) -> Settings:
    root = base.home if source.kind != "local" else Path(source.locator)
    exclusions = set(base.exclusions)
    exclusions.update(str(value) for value in source.config.get("exclusions", []))
    return Settings(
        root=root,
        home=base.home,
        exclusions=frozenset(exclusions),
        chunk_chars=base.chunk_chars,
        chunk_overlap=base.chunk_overlap,
        reconcile_seconds=base.reconcile_seconds,
        embedding_provider=base.embedding_provider,
        embedding_model=base.embedding_model,
        openai_base_url=base.openai_base_url,
        openai_api_key=base.openai_api_key,
    )
