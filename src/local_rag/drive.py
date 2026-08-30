from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .indexer import FileSnapshot, ProgressCallback
from .sources import SourceRecord

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
EXPORTS = {
    GOOGLE_DOC: ("text/plain", ".txt"),
    GOOGLE_SHEET: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    GOOGLE_SLIDES: (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}
MIME_SUFFIXES = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
SUPPORTED_MIMES = frozenset({*EXPORTS, *MIME_SUFFIXES})


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime_type: str
    relative_path: str
    modified_time: str
    version: str
    checksum: str
    web_url: str

    @property
    def fingerprint(self) -> str:
        return "\0".join((self.checksum, self.version, self.modified_time, self.relative_path))


@dataclass(frozen=True)
class DriveChanges:
    changed: Sequence[DriveItem]
    deleted_ids: Sequence[str]
    cursor: str
    full_rescan: bool = False


class DriveBackend(Protocol):
    def full_scan(self, root_id: str) -> tuple[Sequence[DriveItem], str]: ...

    def changes(self, root_id: str, cursor: str) -> DriveChanges: ...

    def download(self, item: DriveItem) -> bytes: ...

    def close(self) -> None: ...


class GoogleDriveBackend:
    """Read-only Google Drive v3 backend. OAuth tokens are referenced, never copied to the DB."""

    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

    def __init__(self, source: SourceRecord):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Google Drive support requires: pip install 'local-rag-mcp[google-drive]'"
            ) from exc
        token = Path(str(source.config.get("token_file", ""))).expanduser()
        if not token.is_file():
            raise ValueError(f"OAuth token file does not exist: {token}")
        if os.name != "nt" and token.stat().st_mode & 0o077:
            raise ValueError(f"OAuth token permissions must be 0600: {token}")
        credentials = Credentials.from_authorized_user_file(str(token), self.SCOPES)
        if not credentials.valid and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:
                raise ValueError("Google OAuth token refresh failed") from exc
            _write_token_atomic(token, credentials.to_json())
        if not credentials.valid:
            raise ValueError("Google OAuth token is invalid; run auth-google again")
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.shared_drive_id = str(source.config.get("shared_drive_id", ""))
        self.exclusions = frozenset(str(v) for v in source.config.get("exclusions", []))

    def _execute(self, request: Any) -> dict[str, Any]:
        try:
            return dict(request.execute(num_retries=5))
        except Exception as exc:
            raise RuntimeError(f"Google Drive API request failed: {type(exc).__name__}") from exc

    def _children(self, folder_id: str) -> list[dict[str, Any]]:
        result, token = [], None
        while True:
            values: dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": (
                    "nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime,"
                    "md5Checksum,version,webViewLink,size,capabilities(canDownload))"
                ),
                "pageSize": 1000,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "pageToken": token,
            }
            if self.shared_drive_id:
                values.update({"corpora": "drive", "driveId": self.shared_drive_id})
            payload = self._execute(self.service.files().list(**values))
            if payload.get("incompleteSearch"):
                raise RuntimeError("Drive returned incompleteSearch; refusing authoritative sync")
            result.extend(payload.get("files", []))
            token = payload.get("nextPageToken")
            if not token:
                return result

    @staticmethod
    def _item(value: dict[str, Any], relative_path: str) -> DriveItem:
        if not value.get("capabilities", {}).get("canDownload", True):
            raise RuntimeError(f"Drive file is not downloadable: {value.get('id', '')}")
        return DriveItem(
            str(value["id"]),
            str(value["name"]),
            str(value["mimeType"]),
            relative_path,
            str(value.get("modifiedTime", "")),
            str(value.get("version", "")),
            str(value.get("md5Checksum", "")),
            str(value.get("webViewLink") or f"https://drive.google.com/open?id={value['id']}"),
        )

    def full_scan(self, root_id: str) -> tuple[Sequence[DriveItem], str]:
        output: list[DriveItem] = []
        pending: list[tuple[str, tuple[str, ...]]] = [(root_id, ())]
        seen = set()
        while pending:
            folder_id, parts = pending.pop()
            if folder_id in seen:
                continue
            seen.add(folder_id)
            for value in self._children(folder_id):
                name = _safe_segment(str(value["name"]))
                relative = "/".join((*parts, name))
                if any(part in self.exclusions for part in Path(relative).parts):
                    continue
                if value["mimeType"] == FOLDER_MIME:
                    pending.append((str(value["id"]), (*parts, name)))
                elif value["mimeType"] in SUPPORTED_MIMES:
                    output.append(self._item(value, relative))
        cursor = self._execute(self.service.changes().getStartPageToken(supportsAllDrives=True))[
            "startPageToken"
        ]
        return output, str(cursor)

    def changes(self, root_id: str, cursor: str) -> DriveChanges:
        changed_ids: set[str] = set()
        deleted: set[str] = set()
        next_token: str | None = cursor
        new_cursor = cursor
        full_rescan = False
        while next_token:
            payload = self._execute(
                self.service.changes().list(
                    pageToken=next_token,
                    pageSize=1000,
                    spaces="drive",
                    includeRemoved=True,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    fields=(
                        "nextPageToken,newStartPageToken,changes(fileId,removed,"
                        "file(id,mimeType,trashed))"
                    ),
                )
            )
            for change in payload.get("changes", []):
                file_id = str(change.get("fileId", ""))
                file_value = change.get("file") or {}
                if change.get("removed") or file_value.get("trashed"):
                    deleted.add(file_id)
                elif file_value.get("mimeType") == FOLDER_MIME:
                    full_rescan = True
                else:
                    changed_ids.add(file_id)
            raw_next_token = payload.get("nextPageToken")
            next_token = str(raw_next_token) if raw_next_token else None
            raw_new_cursor = payload.get("newStartPageToken")
            if raw_new_cursor:
                new_cursor = str(raw_new_cursor)
        # Resolving paths safely across moves is subtle; a small change set triggers a bounded
        # authoritative tree read; the adapter still downloads/extracts changed fingerprints only.
        items, _ = self.full_scan(root_id)
        by_id = {item.id: item for item in items}
        changed = [by_id[file_id] for file_id in changed_ids if file_id in by_id]
        deleted.update(file_id for file_id in changed_ids if file_id not in by_id)
        return DriveChanges(changed, sorted(deleted), new_cursor, full_rescan)

    def download(self, item: DriveItem) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        if item.mime_type in EXPORTS:
            mime_type, _ = EXPORTS[item.mime_type]
            request = self.service.files().export_media(fileId=item.id, mimeType=mime_type)
        else:
            request = self.service.files().get_media(fileId=item.id, supportsAllDrives=True)
        output, done = io.BytesIO(), False
        downloader = MediaIoBaseDownload(output, request, chunksize=4 * 1024 * 1024)
        while not done:
            _, done = downloader.next_chunk(num_retries=5)
        return output.getvalue()

    def close(self) -> None:
        close = getattr(self.service, "close", None)
        if callable(close):
            close()


class DriveAdapter:
    def __init__(self, service: Any, source: SourceRecord, backend: DriveBackend):
        self.service = service
        self.source = source
        self.backend = backend
        self.indexer = service.indexer_for(source)

    def sync(
        self,
        force_index: bool = False,
        reextract: bool = False,
        full: bool = False,
        target: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        known = {
            row["external_id"]: row for row in self.service.db.document_snapshot(self.source.id)
        }
        items: Sequence[DriveItem]
        cursor: str | None
        deleted: set[str]
        mode: str
        if target:
            normalized_target = target.strip("/")
            if not normalized_target or normalized_target in {".", ".."}:
                raise ValueError("Drive target must be a relative file or folder path")
            scanned, _ = self.backend.full_scan(self.source.locator)
            items = [
                item
                for item in scanned
                if item.relative_path == normalized_target
                or item.relative_path.startswith(f"{normalized_target}/")
            ]
            known_in_target = {
                external_id
                for external_id, row in known.items()
                if row["relative_path"] == normalized_target
                or row["relative_path"].startswith(f"{normalized_target}/")
            }
            deleted = known_in_target - {item.id for item in items}
            cursor, mode = self.source.cursor, "target"
        elif full or force_index or not self.source.cursor:
            items, cursor = self.backend.full_scan(self.source.locator)
            deleted = set(known) - {item.id for item in items}
            mode = "full"
        else:
            changes = self.backend.changes(self.source.locator, self.source.cursor)
            if changes.full_rescan:
                items, cursor = self.backend.full_scan(self.source.locator)
                deleted = set(known) - {item.id for item in items}
                mode = "full"
            else:
                items, cursor, deleted, mode = (
                    changes.changed,
                    changes.cursor,
                    set(changes.deleted_ids),
                    "changes",
                )
        report: dict[str, Any] = {
            "source": self.source.name,
            "mode": mode,
            "indexed": 0,
            "unchanged": 0,
            "removed": 0,
            "embedded": 0,
            "warnings": [],
            "errors": [],
        }
        discovered = len(items) + len(deleted)
        processed = searchable = 0
        if progress is not None:
            progress(
                {
                    "phase": "discovering",
                    "discovered": discovered,
                    "processed": 0,
                    "searchable": 0,
                    "remaining": discovered,
                }
            )
        for external_id in deleted:
            row = known.get(external_id)
            if row is not None:
                self._delete_document(int(row["id"]))
                report["removed"] += 1
            processed += 1
            if progress is not None:
                progress(
                    {
                        "phase": "indexing",
                        "discovered": discovered,
                        "processed": processed,
                        "searchable": searchable,
                        "remaining": discovered - processed,
                    }
                )
        for item in items:
            current = known.get(item.id)
            try:
                if current is not None and current["source_version"] == item.fingerprint:
                    if force_index and not reextract:
                        snapshot = FileSnapshot(
                            Path(current["path"]), int(current["size"]), int(current["modified_ns"])
                        )
                        prepared = self.indexer._prepare_cached_reindex(snapshot, current)
                        with self.service.db.transaction() as connection:
                            self.indexer._store(connection, prepared)
                        report["indexed"] += 1
                    elif reextract:
                        self._index(item, current, redownload=False, reextract=True)
                        report["indexed"] += 1
                    else:
                        report["unchanged"] += 1
                else:
                    self._index(item, current, redownload=True)
                    report["indexed"] += 1
                searchable += 1
            except Exception as exc:
                report["errors"].append(f"{item.relative_path}: {exc}")
            processed += 1
            if progress is not None:
                progress(
                    {
                        "phase": "indexing",
                        "discovered": discovered,
                        "processed": processed,
                        "searchable": searchable,
                        "remaining": discovered - processed,
                    }
                )
        embedded = self.indexer.embed_pending(progress)
        report["embedded"] = embedded["embedded"]
        report["warnings"].extend(embedded["warnings"])
        if not target:
            durable_cursor = self.source.cursor if report["errors"] else cursor
            self.service.db.update_source_sync(
                self.source.id, durable_cursor, "; ".join(report["errors"]) or None
            )
        return report

    def _index(
        self,
        item: DriveItem,
        current: Any,
        redownload: bool,
        reextract: bool = False,
    ) -> None:
        raw_dir = self.service.settings.cache_dir / "sources" / self.source.id / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        suffix = EXPORTS.get(item.mime_type, ("", MIME_SUFFIXES.get(item.mime_type, ".bin")))[1]
        safe_id = hashlib.sha256(item.id.encode()).hexdigest()
        pointer = raw_dir / f"{safe_id}{suffix}"
        if redownload or not pointer.is_file():
            content = self.backend.download(item)
            if len(content) > 512 * 1024 * 1024:
                raise ValueError("Drive file exceeds the 512 MiB safety limit")
            descriptor, temporary_name = tempfile.mkstemp(dir=raw_dir, prefix=".download-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, pointer)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        content_hash = _hash_file(pointer)
        if current is not None and current["content_hash"] == content_hash and not reextract:
            prepared = self.indexer._prepare_cached_reindex(
                FileSnapshot(Path(current["path"]), pointer.stat().st_size, 0), current
            )
            prepared = replace(
                prepared,
                relative_path=item.relative_path,
                source_version=item.fingerprint,
                source_url=item.web_url,
            )
            with self.service.db.transaction() as connection:
                self.indexer._store(connection, prepared)
            return
        extracted, artifact = self.indexer._extract_cached(
            pointer, content_hash, use_cache=not reextract
        )
        metadata = {
            "title": Path(item.name).stem,
            "source": self.source.name,
            "source_kind": "google_drive",
            "account": self.source.account,
            "extension": suffix,
            "size": pointer.stat().st_size,
            "modified_ns": 0,
            "content_hash": content_hash,
            "word_count": len(extracted.text.split()),
            "source_positions": len(extracted.spans),
            "modified_time": item.modified_time,
        }
        self.indexer.store_external(
            f"drive://{self.source.id}/{item.id}",
            item.relative_path,
            item.id,
            content_hash,
            extracted,
            artifact,
            metadata,
            item.fingerprint,
            item.web_url,
        )

    def _delete_document(self, document_id: int) -> None:
        from .indexer import _delete_document

        with self.service.db.transaction() as connection:
            _delete_document(connection, document_id, self.service.db)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_segment(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise ValueError("Drive item name is not a safe path segment")
    return value


def authorize_google(token_file: Path, client_secret: Path) -> Path:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive auth requires: pip install 'local-rag-mcp[google-drive]'"
        ) from exc
    payload = json.loads(client_secret.read_text(encoding="utf-8"))
    if not payload.get("installed", {}).get("client_id"):
        raise ValueError("client secret must be a Google OAuth Desktop application JSON")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), GoogleDriveBackend.SCOPES)
    credentials = flow.run_local_server(port=0)
    token = token_file.expanduser().resolve()
    _write_token_atomic(token, credentials.to_json())
    return token


def _write_token_atomic(token: Path, payload: str) -> None:
    token.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        token.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=token.parent, prefix=f".{token.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, token)
        token.chmod(0o600)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
