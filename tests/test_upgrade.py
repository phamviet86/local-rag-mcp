from pathlib import Path
from unittest.mock import patch

from local_rag.config import Settings
from local_rag.service import MultiSourceRAG


def test_incomplete_local_discovery_never_deletes(tmp_path: Path):
    root = tmp_path / "documents"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    (nested / "note.txt").write_text("orchard harvest")
    home = tmp_path / "data"
    service = MultiSourceRAG(Settings(root=home, home=home))
    service.add_local_source("notes", root)
    service.reconcile()
    before = service.db.document_snapshot(service.registry.get("notes").id)
    for error in (FileNotFoundError("secret filename"), PermissionError("secret filename")):
        with patch("local_rag.indexer.os.scandir", side_effect=error):
            report = service.reconcile()
        assert report["sources"][0]["removed"] == 0
        assert report["sources"][0]["errors"]
        assert "secret filename" not in service.registry.get("notes").last_error
        assert [dict(row) for row in service.db.document_snapshot(before[0]["source_id"])] == [
            dict(row) for row in before
        ]
    import os

    real_scan = os.scandir

    def scan(path):
        if Path(path) == nested:
            raise PermissionError("private child")
        return real_scan(path)

    with patch("local_rag.indexer.os.scandir", side_effect=scan):
        assert service.reconcile()["sources"][0]["removed"] == 0
    moved = tmp_path / "relocated"
    root.rename(moved)
    assert service.reconcile()["sources"][0]["errors"]
    assert service.status()["documents"] == 1
    moved.rename(root)
    assert not service.reconcile()["sources"][0]["errors"]
    assert service.registry.get("notes").last_error is None


def test_local_moves_preserve_identity_and_do_not_extract(tmp_path: Path):
    root, home = tmp_path / "root", tmp_path / "data"
    root.mkdir()
    folder = root / "old"
    folder.mkdir()
    for i in range(2):
        (folder / f"note{i}.txt").write_text(f"orchard {i}")
    service = MultiSourceRAG(Settings(root=home, home=home))
    source = service.add_local_source("notes", root)
    service.reconcile()
    original = service.db.document_snapshot(source["id"])
    with service.db.transaction() as connection:
        connection.execute(
            """INSERT INTO relationships
            (source_document_id,target_document_id,relation,evidence_json,actor)
            VALUES(?,?,'related','[]','test')""",
            (original[0]["id"], original[1]["id"]),
        )
    folder.rename(root / "new")
    with patch.object(service.extractor, "extract", side_effect=AssertionError("no extraction")):
        report = service.reconcile()["sources"][0]
    assert report["moved"] == 2 and report["indexed"] == report["removed"] == 0
    updated = service.db.document_snapshot(source["id"])
    assert {row["id"] for row in updated} == {row["id"] for row in original}
    assert all(row["relative_path"].startswith("new/") for row in updated)
    with service.db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM relationships").fetchone()[0] == 1
    # Editing during a move cannot transfer unrelated derived state.
    (root / "new/note0.txt").rename(root / "changed.txt")
    (root / "changed.txt").write_text("different content")
    report = service.reconcile()["sources"][0]
    assert report["moved"] == 0 and report["indexed"] == report["removed"] == 1
    (root / "copy.txt").write_text((root / "changed.txt").read_text())
    service.reconcile()
    (root / "copy.txt").rename(root / "copy-moved.txt")
    report = service.reconcile()["sources"][0]
    assert report["moved"] == 0 and report["indexed"] == report["removed"] == 1


def test_relocate_runs_as_admin_job_and_keeps_ids(tmp_path: Path):
    import pytest

    from local_rag.mcp import MCPServer

    root, home = tmp_path / "old", tmp_path / "home"
    root.mkdir()
    (root / "note.txt").write_text("orchard")
    service = MultiSourceRAG(Settings(root=home, home=home))
    source = service.add_local_source("notes", root)
    service.reconcile()
    old = service.db.document_snapshot(source["id"])[0]
    new = tmp_path / "new"
    root.rename(new)
    for profile in ("reader", "reviewer"):
        with pytest.raises(ValueError, match="not exposed"):
            MCPServer(service, profile).call(
                "relocate_source", {"source": "notes", "new_path": str(new)}
            )
    result = MCPServer(service, "admin").call(
        "relocate_source", {"source": "notes", "new_path": str(new)}
    )
    assert result["state"] == "complete"
    updated = service.db.document_snapshot(source["id"])[0]
    assert updated["id"] == old["id"] and updated["path"] == str(new / "note.txt")
    assert (new / "note.txt").read_text() == "orchard"
    result = service.start_index_job("relocate", "notes", str(root), background=False)
    assert result["state"] == "error"
    assert service.registry.get("notes").locator == str(new)


def test_stable_reference_survives_move_and_relocate(tmp_path: Path):
    import pytest

    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    (root / "note.txt").write_text("orchard identity")
    service = MultiSourceRAG(Settings(root=home, home=home))
    source = service.add_local_source("notes", root)
    service.reconcile()
    hit = service.search("orchard", mode="full_text")["results"][0]
    ref = hit["stable_ref"]
    assert ref.startswith(f"local:{source['id']}:")
    (root / "note.txt").rename(root / "moved.txt")
    service.reconcile()
    assert service.read(ref)["path"] == "moved.txt"
    assert service.metadata(ref)["stable_ref"] == ref
    with pytest.raises(ValueError):
        service.read(ref, source="another-source")
    new = tmp_path / "relocated"
    root.rename(new)
    assert (
        service.start_index_job("relocate", "notes", str(new), background=False)["state"]
        == "complete"
    )
    assert service.read(ref)["text"] == "orchard identity"


def test_vietnamese_fts_keeps_original_text_and_vectors(tmp_path: Path):
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    (root / "note.txt").write_text("hợp đồng đường ĐƯỜNG")
    service = MultiSourceRAG(Settings(root=home, home=home))
    service.add_local_source("notes", root)
    service.reconcile()
    for query in ("hop", "hợp", "duong", "đường", "ĐƯỜNG"):
        hit = service.search(query, mode="full_text")["results"][0]
        assert hit["text"] == "hợp đồng đường ĐƯỜNG"
    evidence = [{"path": "notes:note.txt", "quote": "hợp"}]
    service.add_metadata("notes:note.txt", "tag", "đặc biệt", evidence, "test")
    assert service.search("dac", mode="full_text")["results"]


def test_hit_offsets_and_read_around_match_effective_text(tmp_path: Path):
    from local_rag.mcp import MCPServer

    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    (root / "note.txt").write_text("orchard harvest " * 100)
    service = MultiSourceRAG(Settings(root=home, home=home, chunk_chars=100, chunk_overlap=10))
    service.add_local_source("notes", root)
    service.reconcile()
    for hit in service.search("orchard", mode="full_text")["results"]:
        if hit["chunk_ordinal"] is None:
            continue
        text = service.read(hit["stable_ref"])["text"]
        assert text[hit["start_char"] : hit["end_char"]] == hit["text"]
        assert hit["indexed_at"] and hit["source_modified_at"]
        expanded = MCPServer(service).call(
            "read", {"path": hit["stable_ref"], "around": hit["start_char"], "window": 150}
        )
        assert expanded["text"] == text[expanded["start"] : expanded["end"]]
        assert expanded["start"] <= hit["start_char"] < expanded["end"]


def test_diversify_and_collapse_keep_scoped_locations(tmp_path: Path):
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    text = "\n".join(f"orchard harvest section {i} " * 8 for i in range(10))
    (root / "one.txt").write_text(text)
    (root / "copy.txt").write_text(text)
    service = MultiSourceRAG(Settings(root=home, home=home, chunk_chars=100, chunk_overlap=10))
    service.add_local_source("notes", root)
    service.reconcile()
    results = service.search("orchard", limit=100, mode="full_text", per_document=1)["results"]
    refs = [hit["stable_ref"] for hit in results]
    assert len(refs) == len(set(refs))
    hashes = [hit["chunk_hash"] for hit in results if hit["chunk_hash"]]
    assert len(hashes) == len(set(hashes))
    assert any(
        {location["path"] for location in hit.get("locations", [])} == {"one.txt", "copy.txt"}
        for hit in results
    )
    results = service.search("orchard", folder="one.txt", mode="full_text")["results"]
    assert all(
        location["path"] == "one.txt" for hit in results for location in hit.get("locations", [])
    )
