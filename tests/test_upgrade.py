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
