from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from .coverage import RETRY, load_coverage, save_coverage, source_failure
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
    ancestor_ids: tuple[str, ...] = ()
    path_components: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        return json.dumps(
            [
                "drive-metadata-v2",
                self.checksum,
                self.version,
                self.modified_time,
                self.mime_type,
                self.name,
                self.relative_path,
                self.web_url,
                self.ancestor_ids,
                self.path_components,
            ],
            ensure_ascii=True,
        )


class DriveItemError(RuntimeError):
    """An item or subtree failed; the scan is retryable, never authoritative."""


class DriveListingError(DriveItemError):
    def __init__(self, message: str, items: list[dict[str, Any]]):
        super().__init__(message)
        self.items = items


class DriveSourceError(RuntimeError):
    """Authentication, quota, transport, or configuration failure: stop this source."""


@dataclass(frozen=True)
class DriveScan:
    items: Sequence[DriveItem]
    cursor: str
    errors: Sequence[str] = ()
    failures: Sequence[dict[str, Any]] = ()


@dataclass(frozen=True)
class DriveChanges:
    changed: Sequence[DriveItem]
    deleted_ids: Sequence[str]
    cursor: str
    full_rescan: bool = False
    errors: Sequence[str] = ()
    failures: Sequence[dict[str, Any]] = ()


class DriveBackend(Protocol):
    def full_scan(self, root_id: str) -> DriveScan | tuple[Sequence[DriveItem], str]: ...

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
                "Google Drive support requires: pip install 'phamviet-local-rag-mcp[google-drive]'"
            ) from exc
        token = Path(str(source.config.get("token_file", ""))).expanduser()
        if not token.is_file():
            raise ValueError(f"OAuth token file does not exist: {token}")
        if os.name != "nt" and token.stat().st_mode & 0o077:
            raise ValueError(f"OAuth token permissions must be 0600: {token}")
        load_credentials = cast(
            Callable[[str, Sequence[str]], Any], Credentials.from_authorized_user_file
        )
        credentials = load_credentials(str(token), self.SCOPES)
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
        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError
        from httplib2 import HttpLib2Error

        try:
            return dict(request.execute(num_retries=5))
        except HttpError as exc:
            raise _api_error(exc) from exc
        except (GoogleAuthError, HttpLib2Error, OSError) as exc:
            raise DriveSourceError(
                f"Drive transport/authentication failed: {type(exc).__name__}"
            ) from exc

    def _children(self, folder_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        token = None
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
            try:
                payload = self._execute(self.service.files().list(**values))
            except DriveItemError as exc:
                raise DriveListingError(str(exc), result) from exc
            result.extend(payload.get("files", []))
            if payload.get("incompleteSearch"):
                raise DriveListingError(
                    "Drive returned incompleteSearch; scan must be retried", result
                )
            token = payload.get("nextPageToken")
            if not token:
                return result

    @staticmethod
    def _item(
        value: dict[str, Any],
        relative_path: str,
        ancestor_ids: tuple[str, ...] = (),
        path_components: tuple[str, ...] = (),
    ) -> DriveItem:
        if not value.get("capabilities", {}).get("canDownload", True):
            raise DriveItemError("Drive file is not downloadable")
        return DriveItem(
            str(value["id"]),
            str(value["name"]),
            str(value["mimeType"]),
            relative_path,
            str(value.get("modifiedTime", "")),
            str(value.get("version", "")),
            str(value.get("md5Checksum", "")),
            str(value.get("webViewLink") or f"https://drive.google.com/open?id={value['id']}"),
            ancestor_ids,
            path_components,
        )

    def full_scan(self, root_id: str) -> DriveScan:
        # Capture BEFORE traversal so concurrent edits are replayed by changes().
        cursor = str(
            self._execute(self.service.changes().getStartPageToken(supportsAllDrives=True))[
                "startPageToken"
            ]
        )
        output: list[DriveItem] = []
        errors: list[str] = []
        failures: list[dict[str, Any]] = []
        pending: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [(root_id, (), ())]
        seen: set[str] = set()
        while pending:
            folder_id, parts, ancestors = pending.pop()
            if folder_id in seen:
                continue
            seen.add(folder_id)
            try:
                children = self._children(folder_id)
            except DriveListingError as exc:
                errors.append(f"folder {folder_id!r}: {exc}")
                failures.append(_listing_failure(folder_id, parts, ancestors, "listing", exc))
                children = exc.items
            except DriveItemError as exc:
                errors.append(f"folder {folder_id!r}: {exc}")
                failures.append(_listing_failure(folder_id, parts, ancestors, "listing", exc))
                continue
            for value in children:
                try:
                    name, identifier, mime = value["name"], value["id"], value["mimeType"]
                    if (
                        not all(isinstance(v, str) for v in (name, identifier, mime))
                        or not identifier
                    ):
                        raise ValueError("invalid Drive metadata fields")
                    if name in self.exclusions:
                        continue
                    components = (*parts, name)
                    ids = (*ancestors, folder_id)
                    if mime == FOLDER_MIME:
                        pending.append((identifier, components, ids))
                    elif mime in SUPPORTED_MIMES:
                        relative = "/".join(_display_segment(part) for part in components)
                        output.append(self._item(value, relative, ids, components))
                except (KeyError, TypeError, ValueError, DriveItemError) as exc:
                    errors.append(f"file {value.get('id', '')!r}: {type(exc).__name__}: {exc}")
                    failures.append(
                        _listing_failure(
                            str(value.get("id", "")),
                            (*parts, str(value.get("name", ""))),
                            (*ancestors, folder_id),
                            "metadata",
                            exc,
                        )
                    )
        return DriveScan(output, cursor, errors, failures)

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
                    # Removed changes may omit MIME type; this could be a folder.
                    full_rescan = True
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
        scan = self.full_scan(root_id)
        by_id = {item.id: item for item in scan.items}
        if scan.errors:
            # Never infer deletion from a partial tree, even for changed IDs. Return
            # all readable items and retain the old cursor; retry will use a full scan.
            return DriveChanges(scan.items, (), cursor, errors=scan.errors, failures=scan.failures)
        changed = [by_id[file_id] for file_id in changed_ids if file_id in by_id]
        deleted.update(file_id for file_id in changed_ids if file_id not in by_id)
        return DriveChanges(changed, sorted(deleted), new_cursor, full_rescan)

    def download(self, item: DriveItem) -> bytes:
        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
        from httplib2 import HttpLib2Error

        if item.mime_type in EXPORTS:
            mime_type, _ = EXPORTS[item.mime_type]
            request = self.service.files().export_media(fileId=item.id, mimeType=mime_type)
        else:
            request = self.service.files().get_media(fileId=item.id, supportsAllDrives=True)
        output, done = io.BytesIO(), False
        downloader = MediaIoBaseDownload(output, request, chunksize=4 * 1024 * 1024)
        while not done:
            try:
                _, done = downloader.next_chunk(num_retries=5)
            except HttpError as exc:
                raise _api_error(exc) from exc
            except (GoogleAuthError, HttpLib2Error, OSError) as exc:
                raise DriveSourceError(
                    f"Drive transport/authentication failed: {type(exc).__name__}"
                ) from exc
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
        self.coverage = load_coverage(service.db, source.id)
        self._stage = "index"

    def sync(
        self,
        force_index: bool = False,
        reextract: bool = False,
        full: bool = False,
        target: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        self.service.db.update_source_sync(
            self.source.id, self.source.cursor, "full scan required: interrupted sync"
        )
        self.coverage["running"] = True
        save_coverage(self.service.db, self.source.id, self.coverage)
        try:
            return self._sync(force_index, reextract, full, target, progress)
        except Exception as exc:
            # Persist retry intent even after a source/global failure, then propagate.
            source_failure(self.service.db, self.source.id, type(exc).__name__)
            self.service.db.update_source_sync(
                self.source.id, self.source.cursor, f"full scan required: {type(exc).__name__}"
            )
            raise

    def _sync(
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
        scan_errors: Sequence[str] = ()
        scan_failures: Sequence[dict[str, Any]] = ()
        if target:
            normalized_target = target.strip("/")
            if not normalized_target or normalized_target in {".", ".."}:
                raise ValueError("Drive target must be a relative file or folder path")
            scan = _scan(self.backend.full_scan(self.source.locator))
            scan_errors, scan_failures = scan.errors, scan.failures
            items = [
                item
                for item in scan.items
                if _matches_target(
                    item.id, item.relative_path, item.ancestor_ids, normalized_target
                )
            ]
            known_in_target = {
                external_id
                for external_id, row in known.items()
                if _matches_target(
                    external_id,
                    row["relative_path"],
                    json.loads(row["metadata_json"]).get("drive_ancestor_ids", []),
                    normalized_target,
                )
            }
            deleted = known_in_target - {item.id for item in items}
            cursor, mode = self.source.cursor, "target"
        elif (
            full
            or force_index
            or not self.source.cursor
            or (self.source.last_error or "").startswith("full scan required:")
        ):
            scan = _scan(self.backend.full_scan(self.source.locator))
            items, cursor, scan_errors = scan.items, scan.cursor, scan.errors
            scan_failures = scan.failures
            deleted = set(known) - {item.id for item in items}
            mode = "full"
        else:
            changes = self.backend.changes(self.source.locator, self.source.cursor)
            if changes.full_rescan:
                scan = _scan(self.backend.full_scan(self.source.locator))
                items, cursor, scan_errors = scan.items, scan.cursor, scan.errors
                scan_failures = scan.failures
                deleted = set(known) - {item.id for item in items}
                mode = "full"
            else:
                scan_errors, scan_failures = changes.errors, changes.failures
                items, cursor, deleted, mode = (
                    changes.changed,
                    changes.cursor,
                    set(changes.deleted_ids),
                    "changes",
                )
        if scan_errors:
            deleted = set()
        self.coverage["listing_failures"] = [dict(failure) for failure in scan_failures]
        for failure in self.coverage["listing_failures"]:
            previous = known.get(failure["file_id"])
            self.coverage["files"].pop(failure["file_id"], None)
            if failure["stage"] == "metadata":
                failure["index_state"] = "stale" if previous is not None else "unindexed"
            if previous is not None:
                failure["indexed_path"] = previous["relative_path"]
                failure["indexed_ancestor_ids"] = json.loads(previous["metadata_json"]).get(
                    "drive_ancestor_ids", []
                )
        if not target:
            self.coverage["listing_complete"] = not scan_errors
        self.coverage.pop("source_error", None)
        if mode == "full" and not scan_errors:
            present = {item.id for item in items}
            self.coverage["files"] = {
                key: value for key, value in self.coverage["files"].items() if key in present
            }
        for item in items:
            current = known.get(item.id)
            if (
                current is None
                or current["source_version"] != item.fingerprint
                or force_index
                or reextract
            ):
                self.coverage["files"][item.id] = self._file_coverage(item, current, "pending")
        save_coverage(self.service.db, self.source.id, self.coverage)
        report: dict[str, Any] = {
            "source": self.source.name,
            "mode": mode,
            "indexed": 0,
            "unchanged": 0,
            "removed": 0,
            "embedded": 0,
            "warnings": [],
            "errors": list(scan_errors),
            "complete": False,
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
            self.coverage["files"].pop(external_id, None)
            save_coverage(self.service.db, self.source.id, self.coverage)
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
            self._stage = "index"
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
                self._cleanup_raw(item)
                self.coverage["files"].pop(item.id, None)
                searchable += 1
            except (DriveSourceError, sqlite3.Error, MemoryError):
                raise
            except OSError as exc:
                # Disk/permission failures affect the source, not just this document.
                if exc.errno is not None:
                    raise
                report["errors"].append(f"file {item.id!r}: {type(exc).__name__}")
                self.coverage["files"][item.id] = self._file_coverage(item, current, "failed", exc)
            except Exception as exc:
                # Parser SDKs have heterogeneous exception types. This is the explicit
                # per-document isolation boundary: record failure, retain cursor and retry.
                report["errors"].append(f"file {item.id!r}: {type(exc).__name__}")
                self.coverage["files"][item.id] = self._file_coverage(item, current, "failed", exc)
            save_coverage(self.service.db, self.source.id, self.coverage)
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
        report["complete"] = not report["errors"]
        self.coverage["running"] = False
        save_coverage(self.service.db, self.source.id, self.coverage)
        if not target or report["errors"]:
            durable_cursor = self.source.cursor if report["errors"] else cursor
            error = "; ".join(report["errors"])
            if error and (mode != "changes" or scan_errors):
                error = "full scan required: " + error
            self.service.db.update_source_sync(self.source.id, durable_cursor, error or None)
        else:
            self.service.db.update_source_sync(
                self.source.id, self.source.cursor, self.source.last_error
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
            self._stage = "download_export"
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
        self._stage = "parse"
        content_hash = _hash_file(pointer)
        if current is not None:
            previous_extension = json.loads(current["metadata_json"]).get("extension")
            reextract = reextract or previous_extension != suffix
        if current is not None and current["content_hash"] == content_hash and not reextract:
            prepared = self.indexer._prepare_cached_reindex(
                FileSnapshot(Path(current["path"]), pointer.stat().st_size, 0), current
            )
            prepared = replace(
                prepared,
                automatic={**prepared.automatic, **self._identity_metadata(item, pointer)},
                relative_path=item.relative_path,
                source_version=item.fingerprint,
                source_url=item.web_url,
            )
            self._stage = "index"
            with self.service.db.transaction() as connection:
                self.indexer._store(connection, prepared)
            return
        extracted, artifact = self.indexer._extract_cached(
            pointer, content_hash, use_cache=not reextract
        )
        metadata = {
            **self._identity_metadata(item, pointer),
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
        self._stage = "index"
        self.indexer.store_external(
            f"drive://{self.source.id}/{_display_segment(item.id)}",
            item.relative_path,
            item.id,
            content_hash,
            extracted,
            artifact,
            metadata,
            item.fingerprint,
            item.web_url,
        )

    def _file_coverage(
        self,
        item: DriveItem,
        current: Any,
        status: str,
        error: Exception | None = None,
    ) -> dict[str, Any]:
        previous = json.loads(current["metadata_json"]) if current is not None else {}
        return {
            "file_id": item.id,
            "title": item.name,
            "path": item.relative_path,
            "ancestor_ids": list(item.ancestor_ids),
            "status": status,
            "index_state": "stale" if current is not None else "unindexed",
            "indexed_path": current["relative_path"] if current is not None else None,
            "indexed_ancestor_ids": previous.get("drive_ancestor_ids", []),
            "stage": self._stage if error else "pending",
            "reason": type(error).__name__ if error else None,
            "action": RETRY
            if error
            else "Indexing is pending; check job/index status and retry if interrupted.",
        }

    def _cleanup_raw(self, item: DriveItem) -> None:
        # Only after the document commit: retire this ID's previous export extension.
        # Never follow metadata as a filesystem path.
        raw_dir = self.service.settings.cache_dir / "sources" / self.source.id / "raw"
        suffix = EXPORTS.get(item.mime_type, ("", MIME_SUFFIXES.get(item.mime_type, ".bin")))[1]
        key = hashlib.sha256(item.id.encode()).hexdigest()
        retained = f"{key}{suffix}"
        pattern = re.compile(rf"{key}\.(?:txt|md|pdf|docx|xlsx|pptx|bin)")
        if raw_dir.is_dir():
            for path in raw_dir.iterdir():
                if path.name != retained and pattern.fullmatch(path.name):
                    path.unlink(missing_ok=True)

    @staticmethod
    def _identity_metadata(item: DriveItem, pointer: Path) -> dict[str, Any]:
        return {
            "title": item.name,
            "original_name": item.name,
            "drive_file_id": item.id,
            "drive_parent_id": item.ancestor_ids[-1] if item.ancestor_ids else None,
            "drive_ancestor_ids": list(item.ancestor_ids),
            "drive_path_components": list(item.path_components),
            "drive_mime_type": item.mime_type,
            "drive_web_url": item.web_url,
            "drive_raw_key": pointer.name,
            "extension": pointer.suffix,
            "modified_time": item.modified_time,
        }

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


def _listing_failure(
    identifier: str,
    parts: tuple[str, ...],
    ancestors: tuple[str, ...],
    stage: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "file_id": identifier,
        "title": parts[-1] if parts else None,
        "path": "/".join(_display_segment(part) for part in parts),
        "ancestor_ids": list(ancestors),
        "status": "failed",
        "index_state": "unknown",
        "stage": stage,
        "reason": type(error).__name__,
        "action": RETRY,
    }


def _display_segment(value: str) -> str:
    """URI component for display/scoping only. NEVER used as a filesystem name."""
    if value in {".", ".."}:
        return "%2E" * len(value)
    return quote(value, safe="") or "%EMPTY"


def _scan(value: DriveScan | tuple[Sequence[DriveItem], str]) -> DriveScan:
    # Keep compatibility with existing injected backends.
    return value if isinstance(value, DriveScan) else DriveScan(*value)


def _matches_target(identifier: str, path: str, ancestors: Sequence[str], target: str) -> bool:
    if target.startswith("id:"):
        return target[3:] == identifier or target[3:] in ancestors
    return path == target or path.startswith(f"{target}/")


def _api_error(exc: Any) -> RuntimeError:
    status = int(exc.resp.status)
    # 403 is usually global (scope, quota, API disabled). Only explicit per-file
    # reasons are isolatable; never infer from a message or expose response content.
    try:
        reasons = {
            row.get("reason") for row in json.loads(exc.content).get("error", {}).get("errors", [])
        }
    except (ValueError, TypeError, AttributeError):
        reasons = set()
    if status in {404, 410} or (
        status == 403
        and reasons
        and reasons
        <= {
            "insufficientFilePermissions",
            "fileNotDownloadable",
            "cannotDownloadFile",
            "exportSizeLimitExceeded",
        }
    ):
        return DriveItemError(f"Drive item request failed (HTTP {status})")
    return DriveSourceError(f"Drive source request failed (HTTP {status})")


def authorize_google(token_file: Path, client_secret: Path) -> Path:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive auth requires: pip install 'phamviet-local-rag-mcp[google-drive]'"
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
