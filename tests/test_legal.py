from __future__ import annotations

from local_rag.legal import (
    detect_headings,
    find_numbers,
    is_number_query,
    make_cite_key,
    make_key,
    split_markdown,
    split_sections,
)

LAW = """Chương I
QUY ĐỊNH CHUNG

Điều 1. Phạm vi điều chỉnh

Nghị định này quy định chi tiết theo Điều 5 của Luật Đất đai.

Điều 2. Đối tượng áp dụng

1. Cơ quan nhà nước.

2. Tổ chức, cá nhân.

Chương II
QUẢN LÝ

Điều 3. Nguyên tắc

Thực hiện theo quy định tại Điều 2 Nghị định này.
Điều 2 của Luật này được sửa đổi như sau.

Điều 4. Trách nhiệm

Bộ Tài chính chịu trách nhiệm.
"""


def test_find_numbers_cases() -> None:
    assert find_numbers("theo 66.13/2026/NQ-CP") == ["66.13/2026/NQ-CP"]
    assert find_numbers("Thông tư 24/2026/VBHN-TT-BTC") == ["24/2026/VBHN-TT-BTC"]
    assert find_numbers("135/VBHN-TT-BCT") == ["135/VBHN-TT-BCT"]
    assert find_numbers("ngày 01/04/2025") == []
    assert is_number_query("66.13/2026/NQ-CP")
    assert not is_number_query("quy định 66.13/2026/NQ-CP về")


def test_make_key_folds_diacritics() -> None:
    assert make_key("66.13/2026/NQ-CP") == "66-13-2026-nq-cp"
    assert make_key("01/2026/NĐ-CP") == "01-2026-nd-cp"


def test_headings_reject_cross_references() -> None:
    headings = [h for h in detect_headings(LAW) if h.level == "dieu"]
    assert [h.number for h in headings] == ["1", "2", "3", "4"]
    quoted = "Điều 1. A\n\nNội dung.\n\nĐiều 2. B\n\nNội dung.\n\nĐiều 5 của Luật này sửa đổi.\n"
    assert [h.number for h in detect_headings(quoted)] == ["1", "2"]


def test_split_sections_one_article_each_with_context() -> None:
    sections = split_sections(LAW, "99/2026/NĐ-CP", "Nghị định mẫu")
    assert [s.key_parts[0] for s in sections] == ["dieu-1", "dieu-2", "dieu-3", "dieu-4"]
    assert sections[0].text.startswith("99/2026/NĐ-CP | Nghị định mẫu | Chương I")
    assert sections[2].path[0].startswith("Chương II")
    assert LAW[sections[1].start : sections[1].end].startswith("Điều 2.")


def test_split_by_clause_when_long() -> None:
    body = "Điều 1. Dài\n\n" + "\n\n".join(f"{i}. " + "nội dung " * 40 + "hết." for i in (1, 2, 3))
    text = body + "\n\nĐiều 2. Ngắn\n\nNội dung.\n"
    sections = split_sections(text, None, "", max_chars=500)
    keys = [s.key_parts[0] for s in sections]
    assert keys[:3] == ["dieu-1-khoan-1", "dieu-1-khoan-2", "dieu-1-khoan-3"]
    assert keys[-1] == "dieu-2"


def test_unstructured_falls_back() -> None:
    assert split_sections("Văn bản không có cấu trúc.\n\nChỉ một đoạn.") == []
    assert split_sections("Điều 1. Duy nhất\n\nNội dung.") == []
    assert split_markdown("không có tiêu đề") == []


def test_markdown_sections() -> None:
    sections = split_markdown("# A\n\nx\n\n## B\n\ny\n", "1/ND", "Tên")
    assert len(sections) == 2
    assert sections[1].path == ["A", "B"]


def test_cite_key_uniqueness() -> None:
    taken: set[str] = set()
    first = make_cite_key("1/2026/ND-CP", ["dieu-1"], taken)
    second = make_cite_key("1/2026/ND-CP", ["dieu-1"], taken)
    assert (first, second) == ("1/2026/ND-CP#dieu-1", "1/2026/ND-CP#dieu-1~2")
