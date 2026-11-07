"""list_documents pagination/filters and the read size limits."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import anyio
import pytest

from local_rag.cli import main
from local_rag.config import Settings
from local_rag.mcp import MCPServer, create_sdk_server
from local_rag.service import MultiSourceRAG
from tests.test_drive_regressions import TreeDrive, value
from tests.test_drive_trash import trash_change


@pytest.fixture
def service(tmp_path: Path) -> MultiSourceRAG:
    home = tmp_path / "home"
    settings = Settings(root=home, home=home, ocr_mode="no-ocr")
    settings.save()
    rag = MultiSourceRAG(settings)
    for name, count in (("alpha", 7), ("beta", 6)):
        root = tmp_path / name
        (root / "sub").mkdir(parents=True)
        for index in range(count):
            folder = root / "sub" if index % 2 else root
            (folder / f"{name}-{index}.txt").write_text(f"{name} body {index}", encoding="utf-8")
        rag.add_local_source(name, root)
    (tmp_path / "beta" / "notes.md").write_text("# notes\nbeta md body\n", encoding="utf-8")
    assert not rag.reconcile()["errors"]
    return rag


def collect(service: MultiSourceRAG, limit: int, **filters: object) -> tuple[list[dict], int]:
    seen: list[dict] = []
    cursor = None
    pages = 0
    while True:
        page = service.list_documents(limit=limit, cursor=cursor, **filters)  # type: ignore[arg-type]
        assert len(page["documents"]) <= limit
        seen.extend(page["documents"])
        pages += 1
        assert pages < 100
        cursor = page["next_cursor"]
        if cursor is None:
            return seen, page["total"]


def test_pagination_is_complete_without_duplicates_or_gaps(service: MultiSourceRAG) -> None:
    everything, total = collect(service, 100)
    assert total == len(everything) == 14
    for limit in (1, 2, 3, 5, 13, 14):
        items, page_total = collect(service, limit)
        assert page_total == total
        assert [item["stable_ref"] for item in items] == [item["stable_ref"] for item in everything]
        assert len({item["stable_ref"] for item in items}) == total
    item = everything[0]
    assert set(item) == {
        "stable_ref",
        "source",
        "path",
        "title",
        "media_type",
        "doc_no",
        "url",
        "indexed_at",
        "source_modified_at",
        "trashed",
        "chunk_count",
    }
    assert item["trashed"] is False and item["chunk_count"] >= 1
    assert item["indexed_at"].endswith("Z") and item["stable_ref"].startswith("local:")


def test_pagination_survives_deletion_between_pages(service: MultiSourceRAG) -> None:
    first = service.list_documents(limit=3)
    victim = first["documents"][-1]["stable_ref"].split(":")[-1]
    with service.db.connect() as connection:
        connection.execute("DELETE FROM documents WHERE id=?", (int(victim),))
    cursor, seen = first["next_cursor"], []
    while cursor:
        page = service.list_documents(limit=4, cursor=cursor)
        seen.extend(page["documents"])
        cursor = page["next_cursor"]
    assert len(seen) == 11


def test_filters(service: MultiSourceRAG) -> None:
    beta, _ = collect(service, 10, source="beta")
    assert {item["source"] for item in beta} == {"beta"} and len(beta) == 7
    sub, total = collect(service, 2, source="alpha", folder="sub")
    assert total == len(sub) == 3
    assert all(item["path"].startswith("sub/") for item in sub)
    csv, _ = collect(service, 10, media_type=beta[0]["media_type"])
    assert csv and all(item["media_type"] == beta[0]["media_type"] for item in csv)
    none, total = collect(service, 10, modified_after="2999-01-01")
    assert none == [] and total == 0
    old, _ = collect(service, 10, modified_before="2999-01-01")
    assert len(old) == 14
    unknown, total = collect(service, 10, doc_type="ZZ-NOPE")
    assert unknown == [] and total == 0
    with pytest.raises(ValueError, match="between 1 and 100"):
        service.list_documents(limit=0)
    with pytest.raises(ValueError, match="between 1 and 100"):
        service.list_documents(limit=101)


@pytest.mark.parametrize("cursor", ["!!!", "bm90LWpzb24", "e30", "eyJzIjoxLCJpIjoiMiJ9"])
def test_invalid_cursor_is_an_error(service: MultiSourceRAG, cursor: str) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        service.list_documents(cursor=cursor)


def test_trashed_hidden_by_default_and_shown_on_request(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings = Settings(root=home, home=home, chunk_chars=100, chunk_overlap=10)
    settings.save()
    backend = TreeDrive({"root": [value("a", "alpha"), value("b", "beta")]})
    backend.content = {"a": b"alpha text", "b": b"beta text"}
    rag = MultiSourceRAG(settings, drive_backend_factory=lambda source: backend)
    rag.add_drive_source("fixture", "root", "fixture-account", tmp_path / "token.json")
    assert not rag.reconcile("fixture", full=True)["errors"]
    backend.tree["root"] = [value("a", "alpha", trashed=True), value("b", "beta")]
    backend.change_payload = {"newStartPageToken": "n2", "changes": [trash_change("a")]}
    assert not rag.reconcile("fixture")["errors"]
    visible = rag.list_documents()
    assert [d["stable_ref"] for d in visible["documents"]] == ["drive:b"] and visible["total"] == 1
    everything = rag.list_documents(include_trashed=True)
    by_ref = {d["stable_ref"]: d for d in everything["documents"]}
    assert everything["total"] == 2
    assert by_ref["drive:a"]["trashed"] is True and by_ref["drive:b"]["trashed"] is False
    assert by_ref["drive:a"]["url"] or by_ref["drive:a"]["url"] is None


def test_mcp_and_cli(service: MultiSourceRAG) -> None:
    reader = MCPServer(service, "reader")
    assert "list_documents" in reader.allowed
    page = reader.call("list_documents", {"limit": 5, "source": "alpha"})
    assert len(page["documents"]) == 5 and page["total"] == 7 and page["next_cursor"]
    rest = reader.call("list_documents", {"source": "alpha", "cursor": page["next_cursor"]})
    assert len(rest["documents"]) == 2 and rest["next_cursor"] is None

    sdk = create_sdk_server(service, "reader")

    async def check() -> None:
        result = await sdk.call_tool("list_documents", {"limit": 4})
        assert not result.is_error and len(result.structured_content["documents"]) == 4
        assert result.structured_content["total"] == 14
        with pytest.raises(Exception, match="invalid cursor"):
            await sdk.call_tool("list_documents", {"cursor": "garbage"})

    anyio.run(check)

    def cli(*arguments: str) -> dict:
        output = io.StringIO()
        with redirect_stdout(output):
            assert main(["--home", str(service.settings.home), *arguments]) == 0
        return json.loads(output.getvalue())

    first = cli("list", "--source", "beta", "--limit", "4")
    assert len(first["documents"]) == 4 and first["total"] == 7
    second = cli("list", "--source", "beta", "--limit", "4", "--cursor", first["next_cursor"])
    assert len(second["documents"]) == 3 and second["next_cursor"] is None


def test_read_limits_and_next_start(service: MultiSourceRAG, tmp_path: Path) -> None:
    long_text = "x" * 40001 + "tail"
    big = tmp_path / "alpha" / "big.txt"
    big.write_text(long_text, encoding="utf-8")
    assert not service.reconcile("alpha")["errors"]
    ref = "big.txt"
    first = service.read(ref, source="alpha", length=40000)
    assert first["end"] == 40000 and first["next_start"] == 40000
    assert first["total_chars"] == len(long_text) and len(first["text"]) == 40000
    last = service.read(ref, source="alpha", start=first["next_start"], length=40000)
    assert last["text"] == long_text[40000:] and last["end"] == last["total_chars"]
    assert last["next_start"] is None
    exact = service.read(ref, source="alpha", start=len(long_text) - 10, length=10)
    assert exact["next_start"] is None
    default = service.read(ref, source="alpha")
    assert default["end"] == 12000 and default["next_start"] == 12000
    for bad in ({"length": 40001}, {"length": 0}, {"around": 5, "window": 40001}):
        with pytest.raises(ValueError, match="40000"):
            service.read(ref, source="alpha", **bad)
    assert service.read(ref, source="alpha", around=100, window=40000)["end"] == 40000
    schema = {t["name"]: t for t in MCPServer(service, "reader").tools}["read"]["inputSchema"]
    assert schema["properties"]["length"]["maximum"] == 40000
    assert schema["properties"]["window"]["maximum"] == 40000
