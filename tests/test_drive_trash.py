"""Drive trash vs hard delete, cleanup, service account auth and Markdown export."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from local_rag import drive as drive_module
from local_rag.cli import parser
from local_rag.config import Settings
from local_rag.drive import FOLDER_MIME, GOOGLE_DOC, DriveItemError, GoogleDriveBackend
from local_rag.service import MultiSourceRAG
from tests.test_drive_regressions import TreeDrive, value
from tests.test_workflows import FakeEmbeddings


@pytest.fixture
def make(tmp_path):
    def build(backend, embeddings=None):
        home = tmp_path / "home"
        settings = Settings(root=home, home=home, chunk_chars=100, chunk_overlap=10)
        settings.save()
        service = MultiSourceRAG(
            settings, embeddings=embeddings, drive_backend_factory=lambda source: backend
        )
        service.add_drive_source("fixture", "root", "fixture-account", tmp_path / "token.json")
        return service

    return build


def sync(service, **kwargs):
    result = service.reconcile("fixture", **kwargs)
    assert not result["errors"], result
    return result["sources"][0]


def docs(service):
    return {
        row["external_id"]: row
        for row in service.db.document_snapshot(service.registry.get("fixture").id)
    }


def raw_names(service):
    raw = service.settings.cache_dir / "sources" / service.registry.get("fixture").id / "raw"
    return sorted(path.name for path in raw.iterdir()) if raw.is_dir() else []


def hits(service, query="searchmarker"):
    return {
        r["metadata"]["drive_file_id"]
        for r in service.search(query, source="fixture", mode="full_text")["results"]
    }


def vectors(service):
    with service.db.connect() as connection:
        return connection.execute("SELECT count(*) FROM vectors").fetchone()[0]


def trash_change(file_id, mime=GOOGLE_DOC):
    return {"fileId": file_id, "file": {"id": file_id, "mimeType": mime, "trashed": True}}


def test_trash_hides_everywhere_and_restore_needs_no_download(make):
    backend = TreeDrive({"root": [value("a", "alpha"), value("b", "beta")]})
    backend.content = {"a": b"searchmarker alpha text", "b": b"searchmarker beta text"}
    service = make(backend)
    assert sync(service, full=True)["indexed"] == 2
    cite_key = next(
        r["cite_key"]
        for r in service.search("searchmarker", source="fixture", mode="full_text")["results"]
        if r["metadata"]["drive_file_id"] == "a"
    )
    # Incremental: a change event marks the file as trashed.
    backend.tree["root"] = [value("a", "alpha", trashed=True), value("b", "beta")]
    backend.change_payload = {"newStartPageToken": "n2", "changes": [trash_change("a")]}
    report = sync(service)
    assert report["mode"] == "changes" and report["trashed"] == 1 and report["removed"] == 0
    assert docs(service)["a"]["trashed_at"].endswith("Z")
    assert hits(service) == {"b"}
    with pytest.raises(ValueError, match="no indexed chunk"):
        service.get_chunk(cite_key, None, None, 0, "fixture")
    with pytest.raises(ValueError, match="document_trashed"):
        service.get_chunk(None, "drive:a", 0, 0, "fixture")
    for call in (service.read, service.metadata, lambda ref: service.page_image(ref, 1)):
        with pytest.raises(ValueError, match="document_trashed"):
            call("drive:a")
    with pytest.raises(ValueError, match="document_trashed"):
        service.check_status("drive:a", "2026-01-01")
    assert "alpha" in service.read("drive:a", include_trashed=True)["text"]
    assert service.read("drive:b")["text"]
    # Trash keeps chunks, vectors and the raw cache.
    assert len(raw_names(service)) == 2
    downloads = list(backend.downloads)
    # Restore: same content, found again in the tree and no longer trashed.
    backend.tree["root"] = [value("a", "alpha"), value("b", "beta")]
    backend.change_payload = {
        "newStartPageToken": "n3",
        "changes": [{"fileId": "a", "file": {"id": "a", "mimeType": GOOGLE_DOC, "trashed": False}}],
    }
    report = sync(service)
    assert report["restored"] == 1 and report["indexed"] == 0
    assert backend.downloads == downloads
    assert docs(service)["a"]["trashed_at"] is None
    assert hits(service) == {"a", "b"}
    assert service.get_chunk(cite_key, None, None, 0, "fixture")["chunk"]["text"]


def test_coverage_excludes_trashed_documents(make):
    backend = TreeDrive({"root": [value("a", "alpha"), value("b", "beta")]})
    service = make(backend)
    sync(service, full=True)
    backend.tree["root"] = [value("a", "alpha", trashed=True), value("b", "beta")]
    assert sync(service, full=True)["trashed"] == 1
    listing = service.index_coverage("fixture")
    assert {d["external_id"] for d in listing.get("documents", [])} <= {"b"}
    assert "document_trashed" not in json.dumps(listing)


def test_restore_with_changed_checksum_reindexes(make):
    backend = TreeDrive({"root": [value("a", "alpha", md5Checksum="c1")]})
    backend.content = {"a": b"searchmarker oldtext"}
    service = make(backend)
    sync(service, full=True)
    backend.tree["root"] = [value("a", "alpha", md5Checksum="c1", trashed=True)]
    assert sync(service, full=True)["trashed"] == 1
    backend.tree["root"] = [value("a", "alpha", md5Checksum="c2", version="2")]
    backend.content = {"a": b"searchmarker brandnewtext"}
    before = len(backend.downloads)
    report = sync(service, full=True)
    assert report["restored"] == 1 and report["indexed"] == 1
    assert len(backend.downloads) == before + 1
    assert hits(service, "brandnewtext") == {"a"} and not hits(service, "oldtext")


def test_hard_delete_removes_raw_artifact_and_orphan_vectors(make):
    backend = TreeDrive({"root": [value("a", "alpha"), value("b", "beta")]})
    backend.content = {"a": b"orchard unique alpha content", "b": b"ocean unique beta content"}
    service = make(backend, FakeEmbeddings())
    assert sync(service, full=True)["embedded"] >= 2
    artifact = Path(docs(service)["a"]["artifact_path"])
    assert artifact.is_file() and len(raw_names(service)) == 2
    before = vectors(service)
    backend.tree["root"] = [value("b", "beta")]
    backend.change_payload = {
        "newStartPageToken": "n2",
        "changes": [{"fileId": "a", "removed": True}],
    }
    report = sync(service)
    assert report["removed"] == 1 and report["trashed"] == 0
    assert set(docs(service)) == {"b"}
    assert not artifact.exists()
    assert raw_names(service) == [
        f"{__import__('hashlib').sha256(b'b').hexdigest()}.md",
    ]
    assert vectors(service) < before
    with service.db.connect() as connection:
        orphans = connection.execute(
            """SELECT count(*) FROM vectors v WHERE NOT EXISTS
               (SELECT 1 FROM chunks c WHERE c.chunk_hash=v.chunk_hash)"""
        ).fetchone()[0]
    assert orphans == 0


def test_shared_artifact_survives_delete_of_one_duplicate(make):
    backend = TreeDrive({"root": [value("a", "alpha"), value("b", "beta")]})
    backend.content = {"a": b"identical content", "b": b"identical content"}
    service = make(backend)
    sync(service, full=True)
    artifact = Path(docs(service)["a"]["artifact_path"])
    assert artifact == Path(docs(service)["b"]["artifact_path"])
    backend.tree["root"] = [value("b", "beta")]
    assert sync(service, full=True)["removed"] == 1
    assert artifact.is_file()


def test_scan_error_neither_trashes_nor_deletes(make):
    backend = TreeDrive(
        {
            "root": [
                value("sub", "sub", FOLDER_MIME),
                value("a", "alpha"),
                value("gone", "gone"),
            ],
            "sub": [value("k", "keep")],
        }
    )
    service = make(backend)
    assert sync(service, full=True)["indexed"] == 3
    backend.tree = {
        "root": [value("sub", "sub", FOLDER_MIME), value("a", "alpha", trashed=True)],
        "sub": DriveItemError("fixture listing failure"),
    }
    report = sync(service, full=True)
    assert report["errors"] and report["trashed"] == 0 and report["removed"] == 0
    assert "gc_removed" not in report
    assert set(docs(service)) == {"a", "gone", "k"}
    assert all(row["trashed_at"] is None for row in docs(service).values())
    # Once the listing recovers the same sync applies both effects.
    backend.tree = {
        "root": [value("sub", "sub", FOLDER_MIME), value("a", "alpha", trashed=True)],
        "sub": [value("k", "keep")],
    }
    report = sync(service, full=True)
    assert report["trashed"] == 1 and report["removed"] == 1
    assert set(docs(service)) == {"a", "k"}


def test_trashed_folder_trashes_children_and_restores_without_download(make):
    backend = TreeDrive(
        {
            "root": [value("f", "folder", FOLDER_MIME), value("z", "zeta")],
            "f": [value("x", "ex"), value("y", "why")],
        }
    )
    service = make(backend)
    assert sync(service, full=True)["indexed"] == 3
    downloads = list(backend.downloads)
    # The trashed folder is not traversed; its children are not "missing" either.
    backend.tree = {"root": [value("f", "folder", FOLDER_MIME, trashed=True), value("z", "zeta")]}
    backend.change_payload = {
        "newStartPageToken": "n2",
        "changes": [trash_change("f", FOLDER_MIME)],
    }
    report = sync(service)
    assert report["mode"] == "full" and report["trashed"] == 2 and report["removed"] == 0
    assert hits(service) == {"z"}
    # A later full scan keeps them trashed and does not delete them.
    report = sync(service, full=True)
    assert report["trashed"] == 0 and report["removed"] == 0 and len(docs(service)) == 3
    backend.tree = {
        "root": [value("f", "folder", FOLDER_MIME), value("z", "zeta")],
        "f": [value("x", "ex"), value("y", "why")],
    }
    report = sync(service, full=True)
    assert report["restored"] == 2 and report["indexed"] == 0
    assert backend.downloads == downloads and hits(service) == {"x", "y", "z"}


def test_local_source_delete_purges_artifacts_and_vectors(tmp_path):
    home, folder = tmp_path / "home", tmp_path / "docs"
    folder.mkdir()
    settings = Settings(root=home, home=home, chunk_chars=100, chunk_overlap=10)
    settings.save()
    service = MultiSourceRAG(settings, embeddings=FakeEmbeddings())
    service.add_local_source("notes", folder)
    (folder / "keep.txt").write_text("ocean keeps staying", encoding="utf-8")
    (folder / "gone.txt").write_text("orchard goes away", encoding="utf-8")
    service.reconcile("notes")
    source_id = service.registry.get("notes").id
    gone = next(r for r in service.db.document_snapshot(source_id) if "gone" in r["path"])
    artifact = Path(gone["artifact_path"])
    assert artifact.is_file()
    before = vectors(service)
    (folder / "gone.txt").unlink()
    report = service.reconcile("notes")["sources"][0]
    assert report["removed"] == 1
    assert not artifact.exists() and vectors(service) < before


def test_full_reconcile_collects_orphan_raw_and_artifacts(make):
    backend = TreeDrive({"root": [value("a", "alpha")]})
    service = make(backend)
    sync(service, full=True)
    raw = service.settings.cache_dir / "sources" / service.registry.get("fixture").id / "raw"
    orphan_raw = raw / ("0" * 64 + ".txt")
    orphan_raw.write_bytes(b"left behind")
    extracted = service.settings.extracted_dir
    old, fresh = extracted / "old-orphan.json", extracted / "fresh-orphan.json"
    old.write_text("{}")
    fresh.write_text("{}")
    os.utime(old, (1, 1))
    referenced = Path(docs(service)["a"]["artifact_path"])
    os.utime(referenced, (1, 1))
    assert "gc_removed" not in sync(service)
    report = sync(service, full=True)
    assert report["gc_removed"] == {"raw": 1, "artifacts": 1}
    assert not orphan_raw.exists() and not old.exists()
    assert fresh.exists() and referenced.exists()
    assert (raw / raw_names(service)[0]).is_file()


@contextmanager
def fake_google(credentials):
    """Stand-ins for the optional Google client libraries (not needed to run the suite)."""
    names = (
        "google",
        "google.auth",
        "google.auth.transport",
        "google.auth.transport.requests",
        "google.oauth2",
        "google.oauth2.credentials",
        "google.oauth2.service_account",
        "googleapiclient",
        "googleapiclient.discovery",
    )
    modules = {name: MagicMock() for name in names}
    loader = modules["google.oauth2.service_account"].Credentials.from_service_account_file
    loader.return_value = credentials
    modules["google.oauth2"].service_account = modules["google.oauth2.service_account"]
    build = modules["googleapiclient.discovery"].build
    with patch.dict(sys.modules, modules):
        yield loader, build


def service_account_source(tmp_path, mode=0o600):
    key = tmp_path / "sa.json"
    key.write_text("{}")  # Content is never read: the credential loader is patched.
    key.chmod(mode)
    return key


def test_service_account_auth_enforces_0600_and_hides_key_file(make, tmp_path):
    key = service_account_source(tmp_path)
    service = make(TreeDrive({"root": []}))
    public = service.add_drive_source("sa", "root", "robot", None, service_account_key=key)
    assert public["key_configured"] and not public["token_configured"]
    assert public["config"]["auth"] == "service_account"
    assert "key_file" not in json.dumps(public) and str(key) not in json.dumps(public)
    record = service.registry.get("sa")
    credentials = MagicMock()
    with fake_google(credentials) as (loader, build):
        GoogleDriveBackend(record)
        loader.assert_called_once_with(str(key.resolve()), scopes=GoogleDriveBackend.SCOPES)
        assert build.call_args.kwargs["credentials"] is credentials
        key.chmod(0o644)
        with pytest.raises(ValueError, match="0600"):
            GoogleDriveBackend(record)
        assert loader.call_count == 1


def test_service_account_and_oauth_are_mutually_exclusive(make, tmp_path):
    key = service_account_source(tmp_path)
    service = make(TreeDrive({"root": []}))
    for token, sa in ((None, None), (tmp_path / "t.json", key)):
        with pytest.raises(ValueError, match="exactly one"):
            service.add_drive_source("bad", "root", "acct", token, service_account_key=sa)
    cli = parser()
    base = ["source", "add-drive", "n", "root", "--account", "a"]
    with pytest.raises(SystemExit):
        cli.parse_args([*base, "--token-file", "t", "--service-account-key", "k"])
    with pytest.raises(SystemExit):
        cli.parse_args(base)
    args = cli.parse_args([*base, "--service-account-key", "k"])
    assert args.token_file is None and str(args.service_account_key) == "k"


def test_docs_export_switches_txt_to_markdown_once(make):
    markdown = b"# Title\n\nsearchmarker intro text\n\n## Part\n\nsearchmarker details here\n"
    backend = TreeDrive({"root": [value("d", "doc", md5Checksum="c1")]})
    backend.content = {"d": markdown}
    service = make(backend)
    legacy = {**drive_module.EXPORTS, GOOGLE_DOC: ("text/plain", ".txt")}
    with patch.dict(drive_module.EXPORTS, legacy):
        assert sync(service, full=True)["indexed"] == 1
    assert raw_names(service)[0].endswith(".txt")
    first_id = docs(service)["d"]["id"]
    # Same checksum: previously "unchanged" forever, now reindexed once to the new extension.
    report = sync(service, full=True)
    assert report["indexed"] == 1 and report["unchanged"] == 0
    assert raw_names(service) == [f"{__import__('hashlib').sha256(b'd').hexdigest()}.md"]
    row = docs(service)["d"]
    assert row["id"] == first_id and json.loads(row["metadata_json"])["extension"] == ".md"
    with service.db.connect() as connection:
        paths = [
            json.loads(r[0])
            for r in connection.execute(
                "SELECT section_path FROM chunks WHERE document_id=?", (row["id"],)
            )
        ]
    assert paths and all(paths) and ["Title"] in paths
    again = sync(service, full=True)
    assert again["indexed"] == 0 and again["unchanged"] == 1


def test_moved_file_is_not_trashed_through_stale_ancestors(make):
    backend = TreeDrive(
        {
            "root": [value("A", "a", FOLDER_MIME), value("C", "c", FOLDER_MIME)],
            "A": [value("F", "file"), value("G", "other")],
            "C": [],
        }
    )
    service = make(backend)
    assert sync(service, full=True)["indexed"] == 2
    backend.tree = {
        "root": [value("A", "a", FOLDER_MIME, trashed=True), value("C", "c", FOLDER_MIME)],
        "C": [value("F", "file")],
    }
    first = sync(service, full=True)
    assert first["trashed"] == 1 and first["removed"] == 0
    assert hits(service) == {"F"} and docs(service)["G"]["trashed_at"]
    assert json.loads(docs(service)["F"]["metadata_json"])["drive_ancestor_ids"] == ["root", "C"]
    for _ in range(2):
        again = sync(service, full=True)
        assert again["trashed"] == 0 and again["restored"] == 0
        assert hits(service) == {"F"}


def test_direct_trash_with_live_parent_trashes_only_that_file(make):
    backend = TreeDrive(
        {"root": [value("A", "a", FOLDER_MIME)], "A": [value("F", "f"), value("G", "g")]}
    )
    service = make(backend)
    sync(service, full=True)
    backend.tree["A"] = [value("F", "f", trashed=True), value("G", "g")]
    assert sync(service, full=True)["trashed"] == 1
    assert hits(service) == {"G"}


def test_docs_export_strips_embedded_data_images(make):
    text = (
        b"# T\n\nsearchmarker visible ![][image1] and ![x](data:image/png;base64,QUFB)\n\n"
        b"## P\n\nmore\n\n[image1]: <data:image/png;base64,QUJDREVGRw==>\n"
    )
    backend = TreeDrive({"root": [value("d", "doc")]})
    backend.content = {"d": text}
    service = make(backend)
    sync(service, full=True)
    with service.db.connect() as connection:
        body = " ".join(r[0] for r in connection.execute("SELECT text FROM chunks"))
    assert "searchmarker visible" in body
    assert "base64" not in body and "data:image" not in body


def test_markdown_export_rejection_falls_back_to_txt_without_full_scan_loop(make):
    backend = TreeDrive({"root": [value("d", "doc", md5Checksum="c1")]})
    backend.content = {"d": b"searchmarker plain"}
    original = backend.download

    def download(item):
        backend.export_fallbacks.add(item.id)
        return original(item)

    backend.export_fallbacks = set()
    backend.download = download
    service = make(backend)
    report = sync(service, full=True)
    assert report["indexed"] == 1
    assert any("export_fallback" in w for w in report["warnings"])
    assert raw_names(service)[0].endswith(".txt")
    again = sync(service, full=True)
    assert again["indexed"] == 0 and again["unchanged"] == 1
    assert raw_names(service)[0].endswith(".txt")


def test_markdown_export_http_error_is_classified_as_rejection(tmp_path):
    from local_rag import drive as module

    class Resp:
        status = 400

    class Http(Exception):
        resp = Resp()

    class Downloader:
        def __init__(self, *args, **kwargs):
            pass

        def next_chunk(self, num_retries=0):
            raise Http()

    http_errors = MagicMock()
    http_errors.HttpError = Http
    http = MagicMock()
    http.MediaIoBaseDownload = Downloader
    backend = GoogleDriveBackend.__new__(GoogleDriveBackend)
    backend.export_fallbacks = set()
    calls = []

    class Files:
        def export_media(self, fileId, mimeType):
            calls.append(mimeType)
            return mimeType

    backend.service = MagicMock()
    backend.service.files.return_value = Files()
    item = module.DriveItem("d", "n", GOOGLE_DOC, "n", "", "", "", "")
    with fake_google(MagicMock()):
        sys.modules["googleapiclient.errors"] = http_errors
        sys.modules["googleapiclient.http"] = http
        sys.modules["google.auth.exceptions"] = MagicMock(GoogleAuthError=KeyError)
        sys.modules["httplib2"] = MagicMock(HttpLib2Error=KeyError)
        Http.content = b"{}"
        with pytest.raises(module.DriveSourceError):
            backend.download(item)  # plain-text retry also fails: classified as before
    assert calls == ["text/markdown", "text/plain"]
    assert "d" in backend.export_fallbacks


def test_validity_side_queries_ignore_trashed_duplicates(make):
    backend = TreeDrive({"root": [value("a", "alpha"), value("b", "beta")]})
    backend.content = {"a": b"identical text", "b": b"identical text"}
    service = make(backend)
    sync(service, full=True)
    assert any(
        "DUPLICATE_SHA256" in w for w in service.check_status("drive:a", "2026-01-01")["warnings"]
    )
    backend.tree["root"] = [value("a", "alpha"), value("b", "beta", trashed=True)]
    sync(service, full=True)
    assert not any(
        "DUPLICATE" in w for w in service.check_status("drive:a", "2026-01-01")["warnings"]
    )


def test_artifact_cleanup_resolves_symlinked_home(tmp_path):
    from local_rag.indexer import remove_unreferenced_artifacts

    real = tmp_path / "real"
    (real / "artifacts").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    artifact = real / "artifacts" / "x.json"
    artifact.write_text("{}")
    settings = Settings(root=link, home=link)
    db = MagicMock()
    db.referenced_artifact_paths.return_value = set()
    assert remove_unreferenced_artifacts(db, settings, {str(link / "artifacts" / "x.json")}) == 1
    assert not artifact.exists()


def test_reused_artifact_mtime_is_refreshed(make):
    backend = TreeDrive({"root": [value("a", "alpha")]})
    backend.content = {"a": b"shared body", "b": b"shared body"}
    service = make(backend)
    sync(service, full=True)
    artifact = Path(docs(service)["a"]["artifact_path"])
    os.utime(artifact, (1, 1))
    backend.tree["root"] = [value("a", "alpha"), value("b", "beta")]
    sync(service, full=True)
    assert artifact.stat().st_mtime > 1000
