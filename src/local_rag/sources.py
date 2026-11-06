from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .jobs import iso_z, parse_utc

MIN_POLL_SECONDS = 60.0
DEFAULT_DRIVE_POLL_SECONDS = 300.0
DEFAULT_LOCAL_POLL_SECONDS = 600.0
DEFAULT_FULL_RECONCILE_SECONDS = 43200.0
LAST_FULL_KEY = "last_full_reconcile_at"


def poll_seconds(settings: Settings, source: SourceRecord) -> float:
    """Effective poll interval: source config, else Drive 300 s / local reconcile_seconds."""
    configured = source.config.get("poll_seconds")
    if configured is None:
        configured = (
            DEFAULT_DRIVE_POLL_SECONDS
            if source.kind == "google_drive"
            else (settings.reconcile_seconds or DEFAULT_LOCAL_POLL_SECONDS)
        )
    return max(MIN_POLL_SECONDS, float(configured))


def full_reconcile_seconds(source: SourceRecord) -> float:
    configured = source.config.get("full_reconcile_seconds", DEFAULT_FULL_RECONCILE_SECONDS)
    return max(MIN_POLL_SECONDS, float(configured))


def freshness(settings: Settings, source: SourceRecord) -> dict[str, Any]:
    """Reader-safe sync freshness: UTC ISO 'Z' timestamps and the effective poll interval.

    ``last_synced_at`` is the last successful sync; ``last_attempt_at`` the last attempt (which
    may have failed) and the basis of ``next_sync_at``.
    """
    poll = poll_seconds(settings, source)
    attempt = iso_z(source.last_sync_at)
    next_at = None
    if attempt is not None:
        next_at = iso_z(
            (parse_utc(attempt) + timedelta(seconds=poll)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    return {
        "last_synced_at": iso_z(source.last_success_at),
        "last_attempt_at": attempt,
        "next_sync_at": next_at,
        "poll_seconds": poll,
    }


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
    last_error: str | None = None
    last_sync_at: str | None = None
    created_at: str | None = None
    last_success_at: str | None = None

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
            value.get("last_error"),
            value.get("last_sync_at"),
            value.get("created_at"),
            value.get("last_success_at"),
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
        token_file: Path | None,
        shared_drive_id: str = "",
        exclusions: list[str] | None = None,
        service_account_key: Path | None = None,
    ) -> SourceRecord:
        if not root_id.strip() or not account.strip():
            raise ValueError("Drive root ID and account label are required")
        if (token_file is None) == (service_account_key is None):
            raise ValueError("provide exactly one of an OAuth token file or a service account key")
        config: dict[str, Any] = {
            "shared_drive_id": shared_drive_id,
            "exclusions": exclusions or [],
        }
        if service_account_key is not None:
            config.update(
                {
                    "auth": "service_account",
                    "key_file": str(service_account_key.expanduser().resolve()),
                }
            )
        else:
            assert token_file is not None
            config.update({"auth": "oauth", "token_file": str(token_file.expanduser().resolve())})
        row = self.db.add_source(name, "google_drive", root_id.strip(), account, config)
        return SourceRecord.from_dict(row)

    def list(self, enabled: bool | None = None) -> list[SourceRecord]:
        return [SourceRecord.from_dict(row) for row in self.db.sources(enabled)]

    def get(self, value: str) -> SourceRecord:
        return SourceRecord.from_dict(self.db.source(value))

    def enable(self, value: str, enabled: bool) -> SourceRecord:
        return SourceRecord.from_dict(self.db.set_source_enabled(value, enabled))

    def set_schedule(
        self,
        value: str,
        poll: float | None = None,
        full_reconcile: float | None = None,
    ) -> SourceRecord:
        changes: dict[str, Any] = {}
        if poll is not None:
            if poll < MIN_POLL_SECONDS:
                raise ValueError(f"poll_seconds must be at least {int(MIN_POLL_SECONDS)}")
            changes["poll_seconds"] = poll
        if full_reconcile is not None:
            if full_reconcile < MIN_POLL_SECONDS:
                raise ValueError(f"full_reconcile_seconds must be at least {int(MIN_POLL_SECONDS)}")
            changes["full_reconcile_seconds"] = full_reconcile
        if not changes:
            raise ValueError("provide poll_seconds and/or full_reconcile_seconds")
        return SourceRecord.from_dict(self.db.update_source_config(value, changes))

    def public(self, source: SourceRecord) -> dict[str, Any]:
        config = dict(source.config)
        token_configured = bool(config.pop("token_file", ""))
        key_configured = bool(config.pop("key_file", ""))
        return {
            "id": source.id,
            "name": source.name,
            "kind": source.kind,
            "locator": source.locator,
            "account": source.account,
            "enabled": source.enabled,
            "config": config,
            "token_configured": token_configured,
            "key_configured": key_configured,
            "cursor_configured": bool(source.cursor),
            **freshness(self.settings, source),
            "full_reconcile_seconds": full_reconcile_seconds(source),
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
        chunk_strategy=base.chunk_strategy,
        structure_max_chars=base.structure_max_chars,
        reconcile_seconds=base.reconcile_seconds,
        embedding_provider=base.embedding_provider,
        embedding_model=base.embedding_model,
        openai_base_url=base.openai_base_url,
        openai_api_key=base.openai_api_key,
        ocr_mode=base.ocr_mode,
        docx_document_order=base.docx_document_order,
        markitdown_extensions=base.markitdown_extensions,
    )
