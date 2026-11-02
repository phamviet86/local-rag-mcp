from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from local_rag.cli import main
from local_rag.config import Settings
from local_rag.evaluation import evaluate, load_questions
from local_rag.mcp import MCPServer
from local_rag.service import MultiSourceRAG

DECREE = """NGHỊ ĐỊNH 99/2026/NĐ-CP
Quy định về quản lý tài sản công

Chương I
QUY ĐỊNH CHUNG

Điều 1. Phạm vi điều chỉnh

Nghị định này quy định chi tiết thi hành một số điều của Luật Quản lý tài sản công.

Điều 2. Đối tượng áp dụng

Cơ quan nhà nước và tổ chức sử dụng tài sản công.

Chương II
QUẢN LÝ TÀI SẢN

Điều 3. Nguyên tắc quản lý

Việc quản lý thực hiện theo quy định tại Điều 2 Nghị định này và pháp luật liên quan.
Điều 2 của Luật này được sửa đổi như sau.

Điều 4. Thẩm quyền quyết định

Bộ trưởng Bộ Tài chính quyết định mua sắm tài sản theo Thông tư 24/2026/VBHN-TT-BTC.
"""
PLAIN = "Biên bản họp ngày 01/04/2025.\n\nKhông có cấu trúc điều khoản nào ở đây.\n"


@pytest.fixture
def service(tmp_path: Path) -> MultiSourceRAG:
    home, root = tmp_path / "home", tmp_path / "docs"
    root.mkdir()
    (root / "nghi-dinh.txt").write_text(DECREE, encoding="utf-8")
    (root / "bien-ban.txt").write_text(PLAIN, encoding="utf-8")
    settings = Settings(root=home, home=home)
    settings.save()
    rag = MultiSourceRAG(settings)
    rag.add_local_source("law", root)
    assert not rag.reconcile()["errors"]
    return rag


def keys(service: MultiSourceRAG, query: str, **kwargs: object) -> list[str | None]:
    found = service.search(query, mode="full_text", **kwargs)  # type: ignore[arg-type]
    return [hit["cite_key"] for hit in found["results"]]


def test_structured_chunks_and_cite_keys(service: MultiSourceRAG) -> None:
    with service.db.connect() as connection:
        rows = connection.execute(
            "SELECT c.cite_key,c.section_path,d.doc_no FROM chunks c "
            "JOIN documents d ON d.id=c.document_id WHERE d.doc_no IS NOT NULL "
            "AND c.cite_key NOT LIKE '%#mo-dau%' ORDER BY c.ordinal"
        ).fetchall()
    assert [row["cite_key"] for row in rows] == [f"99/2026/NĐ-CP#dieu-{n}" for n in (1, 2, 3, 4)]
    assert "Chương II" in json.loads(rows[2]["section_path"])[0]


def test_unstructured_uses_window_keys_with_stable_ref(service: MultiSourceRAG) -> None:
    with service.db.connect() as connection:
        key = connection.execute(
            "SELECT cite_key FROM chunks WHERE cite_key LIKE 'local:%' LIMIT 1"
        ).fetchone()[0]
    assert key.startswith("local:") and key.endswith("#c0")


def test_diacritic_free_query_finds_article(service: MultiSourceRAG) -> None:
    assert keys(service, "doi tuong ap dung")[0] == "99/2026/NĐ-CP#dieu-2"
    assert keys(service, "đối tượng áp dụng")[0] == "99/2026/NĐ-CP#dieu-2"


def test_document_number_lookup_ranks_document_first(service: MultiSourceRAG) -> None:
    for query in ("99/2026/NĐ-CP", "99/2026/ND-CP"):
        first = service.search(query, mode="full_text")["results"]
        assert first[0]["doc_no"] == "99/2026/NĐ-CP"
        assert first[0]["matched_via"] == "doc_no"
    boosted = service.search("tài sản", mode="full_text", doc_no="99/2026/NĐ-CP")["results"]
    assert boosted[0]["doc_no"] == "99/2026/NĐ-CP"


def test_match_modes(service: MultiSourceRAG) -> None:
    assert keys(service, "thẩm quyền quyết định", match="phrase")[0].endswith("#dieu-4")
    assert keys(service, "mua sắm xyzabc", match="all") == []
    assert keys(service, "mua sắm xyzabc", match="any")
    with pytest.raises(ValueError, match="match must be"):
        service.search("x", match="fuzzy")


def test_ocr_flag_defaults_false_for_text(service: MultiSourceRAG) -> None:
    hit = service.search("nguyên tắc", mode="full_text")["results"][0]
    assert hit["ocr"] is False and hit["ocr_notice"] is None


def test_get_chunk_with_neighbors_through_mcp(service: MultiSourceRAG) -> None:
    reader = MCPServer(service, "reader")
    result = reader.call("get_chunk", {"cite_key": "99/2026/NĐ-CP#dieu-2", "neighbors": 1})
    assert result["chunk"]["text"].startswith("99/2026/NĐ-CP |")
    assert [n["cite_key"].rsplit("#", 1)[1] for n in result["neighbors"]] == ["dieu-1", "dieu-3"]
    with pytest.raises(ValueError, match="neighbors"):
        reader.call("get_chunk", {"cite_key": "x", "neighbors": 9})
    with pytest.raises(ValueError, match="no indexed chunk"):
        reader.call("get_chunk", {"cite_key": "missing#dieu-1"})


def test_eval_report_and_cli_exit_code(service: MultiSourceRAG, tmp_path: Path) -> None:
    questions = tmp_path / "q.jsonl"
    rows = [
        {
            "id": "q1",
            "question": "doi tuong ap dung",
            "gold": ["99/2026/NĐ-CP#dieu-2"],
            "type": "lookup",
            "diacritics": "no",
        },
        {
            "id": "q2",
            "question": "thẩm quyền quyết định",
            "gold": ["99/2026/NĐ-CP#dieu-4"],
            "type": "lookup",
            "diacritics": "yes",
        },
        {"id": "q3", "question": "không tồn tại", "gold": ["absent#dieu-9"]},
    ]
    questions.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), "utf-8")

    def search(question: str) -> list[dict]:
        return service.search(question, 10, mode="full_text")["results"]

    def exists(key: str) -> bool:
        with service.db.connect() as connection:
            return (
                connection.execute("SELECT 1 FROM chunks WHERE cite_key=?", (key,)).fetchone()
                is not None
            )

    report = evaluate(load_questions(questions), search, exists)
    assert report["recall@1"] == pytest.approx(2 / 3, abs=1e-3)
    assert report["failure_groups"] == {"gold_not_indexed": 1}

    argv = ["--home", str(service.settings.home), "eval", str(questions), "--target", "0.5"]
    with redirect_stdout(io.StringIO()):
        assert main([*argv[:2], *argv[2:]]) == 0
    with redirect_stdout(io.StringIO()):
        assert main([*argv[:-1], "0.99"]) == 1
    with redirect_stdout(io.StringIO()) as out:
        assert main(["--home", str(service.settings.home), "chunk", "99/2026/NĐ-CP#dieu-1"]) == 0
    assert json.loads(out.getvalue())["chunk"]["cite_key"] == "99/2026/NĐ-CP#dieu-1"


def test_window_strategy_keeps_legacy_chunks(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "docs"
    root.mkdir()
    (root / "nghi-dinh.txt").write_text(DECREE, encoding="utf-8")
    settings = Settings(root=home, home=home, chunk_strategy="window")
    settings.save()
    rag = MultiSourceRAG(settings)
    rag.add_local_source("law", root)
    rag.reconcile()
    with rag.db.connect() as connection:
        found = [r[0] for r in connection.execute("SELECT cite_key FROM chunks")]
    assert found and all(key.endswith(f"#c{i}") for i, key in enumerate(found))


EVIDENCE = [{"path": "law:nghi-dinh.txt", "locator": "line:1"}]


def test_validity_filters_status_and_cites_expired(service: MultiSourceRAG, tmp_path: Path) -> None:
    ref = "law:nghi-dinh.txt"
    unknown = service.check_status(ref, "2026-06-01")
    assert unknown["status"] == "unknown"
    assert any(w.startswith("UNKNOWN_VALIDITY") for w in unknown["warnings"])
    assert service.search("tài sản", mode="full_text", valid_on="2026-06-01")["results"] == []

    service.set_validity(
        ref,
        {"effective_from": "2026-01-01", "effective_to": "2027-01-01", "status_source": "test"},
        EVIDENCE,
        "tester",
    )
    assert service.check_status(ref, "2026-12-31")["status"] == "valid"
    assert service.check_status(ref, "2027-01-01")["status"] == "expired"  # half-open end
    assert service.check_status(ref, "2025-12-31")["status"] == "not_yet_effective"
    assert service.search("tài sản", mode="full_text", valid_on="2026-06-01")["results"]
    assert service.search("tài sản", mode="full_text", valid_on="2027-06-01")["results"] == []
    assert service.search("tài sản", mode="full_text", doc_type="NĐ-CP")["results"]
    assert service.search("tài sản", mode="full_text", doc_type="TT")["results"] == []
    with pytest.raises(ValueError, match="valid_on"):
        service.search("tài sản", valid_on="01/06/2026")
    with pytest.raises(ValueError, match="exclusive"):
        service.set_validity(
            ref, {"effective_from": "2026-05-01", "effective_to": "2026-01-01"}, EVIDENCE, "t"
        )


def test_cites_expired_duplicates_and_integrity(service: MultiSourceRAG, tmp_path: Path) -> None:
    root = tmp_path / "docs"
    (root / "thong-tu.txt").write_text(
        "THÔNG TƯ 24/2026/VBHN-TT-BTC\nHướng dẫn\n\nĐiều 1. A\n\nNội dung một.\n\n"
        "Điều 2. B\n\nNội dung hai.\n",
        encoding="utf-8",
    )
    (root / "ban-sao.txt").write_text((root / "nghi-dinh.txt").read_text("utf-8"), "utf-8")
    service.reconcile()
    service.set_validity(
        "law:thong-tu.txt",
        {"effective_from": "2026-01-01", "effective_to": "2026-03-01"},
        [{"path": "law:thong-tu.txt", "locator": "line:1"}],
        "tester",
    )
    report = service.check_status("law:nghi-dinh.txt", "2026-06-01")
    assert any(w.startswith("CITES_EXPIRED: 24/2026/VBHN-TT-BTC") for w in report["warnings"])
    assert any(w.startswith("DUPLICATE_SHA256") for w in report["warnings"])
    assert report["integrity"]["sha256_matches"] is True
    (root / "nghi-dinh.txt").write_text("đã sửa", encoding="utf-8")
    changed = service.check_status("law:nghi-dinh.txt", "2026-06-01")
    assert any(w.startswith("SOURCE_CHANGED") for w in changed["warnings"])


def test_auto_match_falls_back_to_any(service: MultiSourceRAG) -> None:
    result = service.search("mua sắm xyzabc", mode="full_text", match="auto")
    assert result["effective_match"] == "any" and result["results"]
    exact = service.search("mua sắm tài sản", mode="full_text", match="auto")
    assert exact["effective_match"] == "all"
    assert exact["results"][0]["cite_key"].endswith("#dieu-4")


def test_reader_profile_cannot_set_validity(service: MultiSourceRAG) -> None:
    reader = MCPServer(service, "reader")
    assert "check_status" in reader.allowed and "set_validity" not in reader.allowed
    assert "set_validity" in MCPServer(service, "reviewer").allowed


def test_page_image_errors_without_runtime_and_for_text_files(
    service: MultiSourceRAG, tmp_path: Path
) -> None:
    from tests.helpers import write_text_pdf

    with pytest.raises(ValueError, match="PDF documents only"):
        service.page_image("law:nghi-dinh.txt", 1)
    write_text_pdf(tmp_path / "docs" / "native.pdf", "Hello page image")
    service.reconcile()
    with pytest.raises(RuntimeError, match="PDFium runtime missing"):
        service.page_image("law:native.pdf", 1)


def test_png_encoder_round_trip() -> None:
    import struct
    import zlib

    from local_rag.pageimage import _png

    png = _png(bytes([0, 0, 255, 255]) * 4, 2, 2, 8)  # BGRA red pixels
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    length = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + length])
    assert raw[:7] == b"\x00\xff\x00\x00\xff\x00\x00"
