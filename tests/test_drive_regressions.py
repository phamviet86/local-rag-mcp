"""Drive names are metadata; IDs remain the only storage/index identity."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from local_rag.config import Settings
from local_rag.drive import (
    FOLDER_MIME,
    GOOGLE_DOC,
    DriveItemError,
    DriveSourceError,
    GoogleDriveBackend,
    _api_error,
    _display_segment,
)
from local_rag.service import MultiSourceRAG


def value(identifier, name, mime=GOOGLE_DOC, **extra):
    return {"id": identifier, "name": name, "mimeType": mime, "version": "1", **extra}


class TreeDrive(GoogleDriveBackend):
    """Use real traversal, pagination and change resolution without OAuth/network."""

    def __init__(self, tree):
        self.tree = tree
        self.shared_drive_id = ""
        self.exclusions = frozenset()
        self.events = []
        self.downloads = []
        self.fail_download = set()
        self.change_payload = {"newStartPageToken": "after", "changes": []}
        self.start_token = "before"
        self.pages = {}
        self.content = {}
        self.service = self

    def files(self):
        return self

    def changes(self, root_id=None, cursor=None):
        if root_id is not None:
            return super().changes(root_id, cursor)
        return self

    def getStartPageToken(self, **kwargs):
        return ("token", kwargs)

    def list(self, **kwargs):
        return ("children" if "q" in kwargs else "changes", kwargs)

    def _execute(self, request):
        kind, kwargs = request
        self.events.append(kind)
        if kind == "token":
            return {"startPageToken": self.start_token}
        if kind == "changes":
            return self.change_payload
        folder = kwargs["q"].split("'")[1]
        result = self.pages[kwargs["pageToken"]] if kwargs.get("pageToken") else self.tree[folder]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, dict) else {"files": result}

    def download(self, item):
        self.downloads.append(item.id)
        if item.id in self.fail_download:
            raise DriveItemError("fixture export failure")
        return self.content.get(item.id, f"searchmarker {item.id}".encode())

    def close(self):
        pass


@pytest.fixture
def make_service(tmp_path):
    def make(backend):
        home = tmp_path / "home"
        settings = Settings(root=home, home=home, chunk_chars=100, chunk_overlap=10)
        settings.save()
        service = MultiSourceRAG(settings, drive_backend_factory=lambda source: backend)
        service.add_drive_source("fixture", "root", "fixture-account", tmp_path / "token.json")
        return service

    return make


def source_report(service, **kwargs):
    result = service.reconcile("fixture", **kwargs)
    assert not result["errors"], result
    return result["sources"][0]


def rows(service):
    return {
        r["external_id"]: r
        for r in service.db.document_snapshot(service.registry.get("fixture").id)
    }


def test_arbitrary_names_four_slashes_duplicates_and_id_scope(make_service):
    names = [
        "Thuế / kế toán",
        "SOP / biểu mẫu",
        "Mua / bán",
        "Tài sản / bảo trì",
        "duplicate",
        "duplicate",
        ".",
        "..",
        "\\/\0%",
        "",
        "💼漢字",
        "Café",
        "Cafe\u0301",
        "CASE",
        "case",
        "x" * 1000,
    ]
    tree = {
        "root": [
            value("folder-a", "Nhóm / A", FOLDER_MIME),
            value("folder-b", "Nhóm / A", FOLDER_MIME),
        ],
        "folder-a": [value(f"file-{i}", name) for i, name in enumerate(names)],
        "folder-b": [value("other", "duplicate")],
    }
    backend = TreeDrive(tree)
    service = make_service(backend)
    report = source_report(service, full=True)
    assert report["indexed"] == len(names) + 1
    assert report["complete"] and not report["errors"]
    assert backend.events[0] == "token"
    documents = rows(service)
    raw = service.settings.cache_dir / "sources" / service.registry.get("fixture").id / "raw"
    assert len(list(raw.iterdir())) == len(documents)
    for i, name in enumerate(names):
        row = documents[f"file-{i}"]
        metadata = json.loads(row["metadata_json"])
        assert row["title"] == metadata["original_name"] == name
        assert metadata["drive_file_id"] == f"file-{i}"
        assert metadata["drive_parent_id"] == "folder-a"
        assert metadata["drive_ancestor_ids"] == ["root", "folder-a"]
        assert metadata["drive_path_components"] == ["Nhóm / A", name]
        assert row["source_url"].endswith(f"id=file-{i}")
        key = metadata["drive_raw_key"]
        assert re.fullmatch(r"[0-9a-f]{64}\.txt", key)
        assert (raw / key).resolve().parent == raw.resolve()
    results = service.search(
        "searchmarker", source="fixture", folder="id:folder-b", mode="full_text"
    )["results"]
    assert len(results) == 1 and results[0]["metadata"]["drive_file_id"] == "other"
    first_ids = {key: row["id"] for key, row in documents.items()}
    assert source_report(service, full=True)["unchanged"] == len(documents)
    assert source_report(service)["indexed"] == 0
    assert {key: row["id"] for key, row in rows(service).items()} == first_ids
    assert len(backend.downloads) == len(documents)
    # Folder IDs distinguish duplicate sibling names for targeted reindex as well.
    assert source_report(service, target="id:folder-b", force_index=True)["indexed"] == 1


@pytest.mark.parametrize("failure", ["download", "parse"])
def test_one_bad_file_continues_and_retry_recovers(make_service, failure):
    backend = TreeDrive(
        {"root": [value("first", "valid"), value("bad", "bad / file"), value("last", "valid")]}
    )
    service = make_service(backend)
    if failure == "download":
        backend.fail_download.add("bad")
        report = source_report(service, full=True)
    else:
        original = service.extractor.extract
        bad_key = hashlib.sha256(b"bad").hexdigest()

        def extract(path):
            if path.stem == bad_key:
                raise ValueError("invalid fixture document")
            return original(path)

        with patch.object(service.extractor, "extract", side_effect=extract):
            report = source_report(service, full=True)
    assert report["indexed"] == 2 and not report["complete"]
    assert len(report["errors"]) == 1
    assert set(rows(service)) == {"first", "last"}
    assert service.registry.get("fixture").cursor is None
    backend.fail_download.clear()
    retried = source_report(service)
    assert retried["indexed"] == 1 and retried["unchanged"] == 2
    assert retried["complete"] and set(rows(service)) == {"first", "bad", "last"}
    assert service.registry.get("fixture").cursor == "before"


@pytest.mark.parametrize("mode", ["full", "incremental", "target"])
def test_partial_listing_no_false_deletion_or_cursor_and_durable_retry(make_service, mode):
    backend = TreeDrive(
        {
            "root": [value("folder", "folder", FOLDER_MIME), value("visible", "visible")],
            "folder": [value("hidden", "old")],
        }
    )
    service = make_service(backend)
    source_report(service, full=True)
    old_ids = {key: row["id"] for key, row in rows(service).items()}
    backend.tree["folder"] = DriveItemError("temporary inaccessible folder")
    backend.tree["root"].append(value("new", "new"))
    backend.start_token = "later"
    backend.change_payload = {
        "newStartPageToken": "after",
        "changes": [
            {"fileId": "hidden", "file": {"mimeType": GOOGLE_DOC}},
            {"fileId": "new", "file": {"mimeType": GOOGLE_DOC}},
        ],
    }
    kwargs = {"full": True} if mode == "full" else {"target": "id:root"} if mode == "target" else {}
    report = source_report(service, **kwargs)
    assert report["indexed"] == 1 and report["removed"] == 0 and not report["complete"]
    assert set(rows(service)) == {"visible", "hidden", "new"}
    assert service.registry.get("fixture").cursor == "before"
    # No change IDs remain. A full retry is durable across service restarts.
    backend.change_payload = {"newStartPageToken": "after", "changes": []}
    backend.tree["folder"] = [value("hidden", "old"), value("recovered", "recovered")]
    restarted = MultiSourceRAG(service.settings, drive_backend_factory=lambda source: backend)
    report = source_report(restarted)
    assert report["mode"] == "full" and report["complete"]
    assert set(rows(restarted)) == {"visible", "hidden", "new", "recovered"}
    assert all(rows(restarted)[key]["id"] == identifier for key, identifier in old_ids.items())
    assert restarted.registry.get("fixture").cursor == "later"


def test_metadata_failure_isolated_and_incomplete_search_never_authoritative(make_service):
    backend = TreeDrive(
        {
            "root": [
                value("denied", "blocked", capabilities={"canDownload": False}),
                {"id": "malformed", "mimeType": GOOGLE_DOC},
                value("valid", "okay"),
            ]
        }
    )
    service = make_service(backend)
    report = source_report(service, full=True)
    assert report["indexed"] == 1 and len(report["errors"]) == 2
    backend.tree["root"] = {"incompleteSearch": True, "files": []}
    report = source_report(service, full=True)
    assert report["removed"] == 0 and not report["complete"]
    assert set(rows(service)) == {"valid"}


def test_partial_pagination_retries_without_cleanup(make_service):
    backend = TreeDrive(
        {
            "root": [value("folder", "folder", FOLDER_MIME), value("ok", "ok")],
            "folder": [value("keep", "keep")],
        }
    )
    service = make_service(backend)
    source_report(service, full=True)
    backend.tree["folder"] = {"files": [value("page-one", "first page")], "nextPageToken": "p2"}
    backend.pages["p2"] = DriveItemError("page two failed")
    report = source_report(service, full=True)
    assert not report["complete"] and report["removed"] == 0
    assert set(rows(service)) == {"ok", "keep", "page-one"}
    backend.pages["p2"] = {"files": [value("keep", "keep")]}
    assert source_report(service)["complete"]
    assert set(rows(service)) == {"ok", "keep", "page-one"}


def test_rename_move_legacy_metadata_and_extension_preserve_identity(make_service):
    backend = TreeDrive({"root": [value("file", "legacy.txt", "text/plain")]})
    backend.content["file"] = b"stable content"
    service = make_service(backend)
    source_report(service, full=True)
    original = rows(service)["file"]
    # Recreate the previous release's metadata/fingerprint, keeping its raw cache and index.
    legacy_metadata = json.loads(original["metadata_json"])
    for key in list(legacy_metadata):
        if key.startswith("drive_") or key == "original_name":
            del legacy_metadata[key]
    legacy_metadata["title"] = "legacy"
    with service.db.transaction() as connection:
        connection.execute(
            "UPDATE documents SET source_version=?,metadata_json=?,title=? WHERE id=?",
            (
                "\0".join(("", "1", "", "legacy.txt")),
                json.dumps(legacy_metadata),
                "legacy",
                original["id"],
            ),
        )
    # Same content, different name, parent, URL and parser extension.
    backend.tree = {
        "root": [value("parent", "new / folder", FOLDER_MIME)],
        "parent": [
            value("file", "Đổi / tên.md", "text/markdown", webViewLink="https://example.test/file")
        ],
    }
    source_report(service, full=True)
    updated = rows(service)["file"]
    metadata = json.loads(updated["metadata_json"])
    assert updated["id"] == original["id"] and updated["path"] == original["path"]
    assert updated["title"] == metadata["original_name"] == "Đổi / tên.md"
    assert metadata["drive_parent_id"] == "parent" and metadata["extension"] == ".md"
    assert updated["source_url"] == "https://example.test/file"
    raw = service.settings.cache_dir / "sources" / service.registry.get("fixture").id / "raw"
    assert [path.name for path in raw.iterdir()] == [hashlib.sha256(b"file").hexdigest() + ".md"]
    assert source_report(service, full=True)["unchanged"] == 1
    assert service.search("stable", source="fixture", folder="id:parent", mode="full_text")[
        "results"
    ]


@pytest.mark.parametrize("identifier", ["../escape", "a/b", "a%2Fb", "a\\b", ".", "..", "\0"])
def test_untrusted_id_never_becomes_filesystem_path(make_service, identifier):
    backend = TreeDrive({"root": [value(identifier, "../../title")]})
    service = make_service(backend)
    assert source_report(service, full=True)["indexed"] == 1
    metadata = json.loads(rows(service)[identifier]["metadata_json"])
    assert metadata["drive_raw_key"] == hashlib.sha256(identifier.encode()).hexdigest() + ".txt"
    assert metadata["original_name"] == "../../title"
    assert _display_segment("a/b") != _display_segment("a%2Fb")


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (401, "authError", DriveSourceError),
        (403, "insufficientPermissions", DriveSourceError),
        (403, "rateLimitExceeded", DriveSourceError),
        (429, "rateLimitExceeded", DriveSourceError),
        (500, "backendError", DriveSourceError),
        (404, "notFound", DriveItemError),
        (403, "insufficientFilePermissions", DriveItemError),
    ],
)
def test_http_global_vs_item_failure(status, reason, expected):
    exc = SimpleNamespace(
        resp=SimpleNamespace(status=status),
        content=json.dumps({"error": {"errors": [{"reason": reason}]}}).encode(),
    )
    assert isinstance(_api_error(exc), expected)


@pytest.mark.parametrize(
    "error", [DriveSourceError("auth failed"), sqlite3.OperationalError("db failed")]
)
def test_global_failure_stops_source_and_keeps_cursor(make_service, error):
    backend = TreeDrive({"root": [value("first", "first"), value("last", "last")]})
    service = make_service(backend)
    with patch.object(backend, "download", side_effect=error) as download:
        report = service.reconcile("fixture", full=True)
    assert report["errors"] and not report["sources"]
    assert download.call_count == 1
    assert service.registry.get("fixture").cursor is None
    assert service.registry.get("fixture").last_error.startswith("full scan required:")


def test_incremental_one_item_failure_replays_old_cursor(make_service):
    backend = TreeDrive({"root": [value("bad", "old"), value("ok", "old")]})
    service = make_service(backend)
    source_report(service, full=True)
    backend.tree["root"] = [value("bad", "new", version="2"), value("ok", "new", version="2")]
    backend.change_payload = {
        "newStartPageToken": "after",
        "changes": [
            {"fileId": identifier, "file": {"mimeType": GOOGLE_DOC}} for identifier in ("bad", "ok")
        ],
    }
    backend.fail_download.add("bad")
    report = source_report(service)
    assert report["indexed"] == 1 and not report["complete"]
    assert service.registry.get("fixture").cursor == "before"
    backend.fail_download.clear()
    report = source_report(service)
    assert report["indexed"] == 1 and report["unchanged"] == 1 and report["complete"]
    assert service.registry.get("fixture").cursor == "after"
    assert len(rows(service)) == 2


def test_search_mcp_partial_zero_matches_restart_and_repair(make_service):
    from local_rag.mcp import MCPServer

    backend = TreeDrive({"root": [value("good", "Good"), value("bad", "Broken / tài liệu")]})
    backend.fail_download.add("bad")
    service = make_service(backend)
    source_report(service, full=True)
    service = MultiSourceRAG(service.settings, drive_backend_factory=lambda source: backend)
    reader = MCPServer(service, "reader")
    for query in ("searchmarker", "nonexistentxyz"):
        result = reader.call("search", {"query": query, "source": "fixture", "mode": "full_text"})
        assert bool(result["results"]) == (query == "searchmarker")
        assert result["coverage"]["status"] == "partial" and result["warnings"]
        issue = result["coverage"]["issues"][0]
        assert issue["title"] == "Broken / tài liệu" and issue["file_id"] == "bad"
        assert issue["index_state"] == "unindexed" and issue["stage"] == "download_export"
        assert issue["reason"] == "DriveItemError" and issue["action"]
        assert result["coverage"]["sources"][0]["failed_files"] == 1
    detail = reader.call("index_coverage", {"source": "fixture"})
    assert detail["total_issues"] == 1
    backend.fail_download.clear()
    source_report(service)
    result = reader.call("search", {"query": "searchmarker", "mode": "full_text"})
    assert result["coverage"]["status"] == "complete" and not result["coverage"]["issues"]
    assert not result["warnings"]


def test_stale_failed_update_is_visible_in_old_and_new_scope(make_service):
    backend = TreeDrive(
        {
            "root": [value("old-folder", "Old", FOLDER_MIME)],
            "old-folder": [value("file", "Original")],
        }
    )
    service = make_service(backend)
    source_report(service, full=True)
    backend.tree = {
        "root": [value("new-folder", "New", FOLDER_MIME)],
        "new-folder": [value("file", "Changed", version="2")],
    }
    backend.fail_download.add("file")
    source_report(service, full=True)
    for folder in ("id:old-folder", "id:new-folder"):
        result = service.search("searchmarker", source="fixture", folder=folder, mode="full_text")
        assert result["coverage"]["status"] == "partial"
        issue = result["coverage"]["issues"][0]
        assert issue["index_state"] == "stale" and issue["title"] == "Changed"
        assert issue["indexed_path"] == "Old/Original"
    assert service.search(
        "searchmarker", source="fixture", folder="id:old-folder", mode="full_text"
    )["results"]
    backend.fail_download.clear()
    source_report(service)
    assert service.index_coverage("fixture")["status"] == "complete"


def test_unrelated_source_and_folder_errors_not_disclosed(make_service, tmp_path):
    backend = TreeDrive(
        {
            "root": [value("a", "A", FOLDER_MIME), value("b", "B", FOLDER_MIME)],
            "a": [value("bad", "private failed title")],
            "b": [value("ok", "Okay")],
        }
    )
    backend.fail_download.add("bad")
    service = make_service(backend)
    source_report(service, full=True)
    result = service.search("searchmarker", source="fixture", folder="id:b", mode="full_text")
    assert result["coverage"]["status"] == "complete" and result["coverage"]["total_issues"] == 0
    other = tmp_path / "other"
    other.mkdir()
    (other / "local.txt").write_text("local marker")
    service.add_local_source("other", other)
    service.reconcile("other")
    result = service.search("marker", source="other", mode="full_text")
    assert not result["coverage"]["sources"] and not result["coverage"]["issues"]
    assert "private failed title" not in json.dumps(result)


def test_coverage_pagination_and_deletion_clears_failed_unindexed_files(make_service):
    backend = TreeDrive({"root": [value(str(i), f"File {i}") for i in range(13)]})
    backend.fail_download = {str(i) for i in range(13)}
    service = make_service(backend)
    source_report(service, full=True)
    coverage = service.search("absent", mode="full_text")["coverage"]
    assert len(coverage["issues"]) == 10 and coverage["next_offset"] == 10
    rest = service.index_coverage(offset=10)
    assert len(rest["issues"]) == 3 and rest["next_offset"] is None
    assert {
        item["file_id"] for item in coverage["issues"] + rest["issues"]
    } == backend.fail_download
    backend.tree["root"] = []
    assert source_report(service)["complete"]
    assert service.index_coverage()["total_issues"] == 0


def test_metadata_and_listing_coverage_and_pending_interruption(make_service):
    from local_rag.coverage import load_coverage

    backend = TreeDrive(
        {
            "root": [
                value("denied", "Not downloadable", capabilities={"canDownload": False}),
                value("ok", "Okay"),
            ]
        }
    )
    service = make_service(backend)
    source_report(service, full=True)
    issue = service.index_coverage()["issues"][0]
    assert issue["stage"] == "metadata" and issue["index_state"] == "unindexed"
    backend.tree["root"] = [value("denied", "Now downloadable"), value("ok", "Okay")]
    observed = []

    def interrupt(item):
        observed.append(service.index_coverage())
        raise KeyboardInterrupt("simulate process interruption")

    with patch.object(backend, "download", side_effect=interrupt), pytest.raises(KeyboardInterrupt):
        service.reconcile("fixture", full=True)
    assert observed[0]["sources"][0]["pending_files"] == 1
    state = load_coverage(service.db, service.registry.get("fixture").id)
    assert state["files"]["denied"]["status"] == "pending"
    restarted = MultiSourceRAG(service.settings, drive_backend_factory=lambda source: backend)
    assert restarted.index_coverage()["status"] == "partial"
    assert source_report(restarted)["mode"] == "full"
    assert restarted.index_coverage()["status"] == "complete"


def test_removed_folder_reconciles_descendants(make_service):
    backend = TreeDrive(
        {"root": [value("folder", "folder", FOLDER_MIME)], "folder": [value("file", "nested")]}
    )
    service = make_service(backend)
    source_report(service, full=True)
    backend.tree["root"] = []
    backend.change_payload = {
        "newStartPageToken": "after",
        "changes": [{"fileId": "folder", "removed": True}],
    }
    report = source_report(service)
    assert report["removed"] == 1 and not rows(service)


def test_same_content_rename_refreshes_metadata_from_cached_artifact(make_service):
    backend = TreeDrive({"root": [value("file", "Original")]})
    service = make_service(backend)
    source_report(service, full=True)
    original = rows(service)["file"]
    backend.tree["root"] = [
        value("file", "Renamed / nguyên tên", webViewLink="https://example.test/new")
    ]
    backend.fail_download.add("file")
    report = source_report(service, full=True)
    assert report["errors"] and service.registry.get("fixture").cursor == "before"
    backend.fail_download.clear()
    # No change feed entries: the failed full scan must still be retried in full.
    with patch.object(service.extractor, "extract", side_effect=AssertionError("must use cache")):
        assert source_report(service)["mode"] == "full"
    updated = rows(service)["file"]
    assert updated["id"] == original["id"] and updated["content_hash"] == original["content_hash"]
    assert updated["title"] == "Renamed / nguyên tên"
    assert updated["source_url"] == "https://example.test/new"
    assert json.loads(updated["metadata_json"])["original_name"] == updated["title"]
    assert service.index_coverage()["status"] == "complete"


def test_per_document_index_error_rolls_back_and_continues(make_service):
    from local_rag.indexer import Indexer

    backend = TreeDrive({"root": [value("bad", "Bad"), value("good", "Good")]})
    service = make_service(backend)
    original = Indexer._store

    def store(indexer, connection, document):
        if document.external_id == "bad":
            original(indexer, connection, document)
            raise ValueError("simulated invalid document after write")
        return original(indexer, connection, document)

    with patch.object(Indexer, "_store", new=store):
        report = source_report(service, full=True)
    assert report["indexed"] == 1 and set(rows(service)) == {"good"}
    assert service.index_coverage()["issues"][0]["stage"] == "index"
    assert source_report(service)["complete"] and set(rows(service)) == {"bad", "good"}
