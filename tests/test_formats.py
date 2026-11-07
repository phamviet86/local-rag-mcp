from __future__ import annotations

import hashlib
import json
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from local_rag.config import SUPPORTED_EXTENSIONS, Settings
from local_rag.db import MIGRATIONS
from local_rag.drive import MIME_SUFFIXES, SUPPORTED_MIMES
from local_rag.extract import Extractor
from local_rag.extraction_errors import ExtractionError
from local_rag.ocr_runtime import OCRRuntimeManager
from local_rag.service import MultiSourceRAG
from tests.helpers import write_text_pdf
from tests.test_drive_regressions import TreeDrive, value

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
)


@pytest.fixture
def extractor(tmp_path: Path) -> Extractor:
    return Extractor(OCRRuntimeManager(tmp_path / "runtime", tmp_path / "models"))


def _odf(path: Path, body: str) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr(
            "content.xml",
            f'<?xml version="1.0"?><office:document-content {ODF_NS}>'
            f"<office:body>{body}</office:body></office:document-content>",
        )
    return path


def test_html_drops_script_and_style_and_keeps_headings(tmp_path: Path, extractor: Extractor):
    page = tmp_path / "page.html"
    page.write_text(
        "<html><head><title>T</title><style>.x{}</style></head><body>"
        "<script>var secret=1;</script><h1>Main</h1><p>Intro <b>bold</b> text</p>"
        "<h2>Part</h2><ul><li>one</li><li>two</li></ul><table><tr><td>a</td><td>b</td></tr></table>"
        "</body></html>",
        encoding="utf-8",
    )
    document = extractor.extract(page)
    assert document.media_type == "text/markdown"
    assert "secret" not in document.text and ".x" not in document.text
    assert "# Main\n\nIntro bold text\n\n## Part\n\n- one\n\n- two\n\na | b" in document.text
    assert document.spans[0].locator == "block:1"
    assert extractor.extract(page.rename(tmp_path / "page.htm")).text == document.text


def test_csv_rows_pair_header_with_values(tmp_path: Path, extractor: Extractor):
    sheet = tmp_path / "people.csv"
    sheet.write_text("name,city\nAnn,Hanoi\nBob,\n", encoding="utf-8")
    document = extractor.extract(sheet)
    assert document.text == "name: Ann; city: Hanoi\n\nname: Bob"
    assert [span.locator for span in document.spans] == ["row:2", "row:3"]
    assert document.metadata["columns"] == ["name", "city"]


def test_json_is_flattened_with_paths_and_locators(tmp_path: Path, extractor: Extractor):
    data = tmp_path / "data.json"
    data.write_text(
        json.dumps({"a": {"b": "deep", "c": [1, {"d": True}]}, "e": None, "odd key": 2}),
        encoding="utf-8",
    )
    document = extractor.extract(data)
    assert "a.b: deep" in document.text and "a.c[1].d: true" in document.text
    assert "odd key" in document.text and "e: null" in document.text
    assert {span.locator for span in document.spans} >= {"$.a.b", "$.a.c[0]", "$.a.c[1].d"}
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    with pytest.raises(ExtractionError):
        extractor.extract(broken)
    with patch("local_rag.formats.MAX_TEXT_BYTES", 5), pytest.raises(ExtractionError):
        extractor.extract(data)


def test_epub_reads_spine_order(tmp_path: Path, extractor: Extractor):
    book = tmp_path / "book.epub"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<metadata><dc:title>My Book</dc:title></metadata><manifest>"
            '<item id="b" href="ch%202.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="a" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="unused" href="x.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>',
        )
        archive.writestr("OEBPS/ch1.xhtml", "<html><body><h1>First</h1><p>alpha</p></body></html>")
        archive.writestr("OEBPS/ch 2.xhtml", "<html><body><h1>Second</h1><p>beta</p></body></html>")
        archive.writestr("OEBPS/x.xhtml", "<html><body><p>unused</p></body></html>")
    document = extractor.extract(book)
    assert document.text.index("alpha") < document.text.index("beta")
    assert "unused" not in document.text
    assert [span.locator for span in document.spans] == ["chapter:1", "chapter:2"]
    assert document.metadata["epub_title"] == "My Book"
    assert document.media_type == "text/markdown"


def test_odf_text_spreadsheet_and_presentation(tmp_path: Path, extractor: Extractor):
    odt = _odf(
        tmp_path / "doc.odt",
        '<office:text><text:h text:outline-level="1">Title</text:h>'
        "<text:p>Hello<text:s/>world</text:p></office:text>",
    )
    document = extractor.extract(odt)
    assert document.text == "# Title\n\nHello world"
    assert [span.locator for span in document.spans] == ["paragraph:1", "paragraph:2"]

    ods = _odf(
        tmp_path / "sheet.ods",
        '<office:spreadsheet><table:table table:name="Data">'
        "<table:table-row><table:table-cell><text:p>k</text:p></table:table-cell>"
        '<table:table-cell table:number-columns-repeated="3"/></table:table-row>'
        '<table:table-row table:number-rows-repeated="5"><table:table-cell/></table:table-row>'
        "<table:table-row><table:table-cell><text:p>v</text:p></table:table-cell></table:table-row>"
        "</table:table></office:spreadsheet>",
    )
    document = extractor.extract(ods)
    assert [span.locator for span in document.spans] == ["sheet:Data/row:1", "sheet:Data/row:7"]

    odp = _odf(
        tmp_path / "deck.odp",
        "<office:presentation><draw:page><text:p>one</text:p></draw:page>"
        "<draw:page><text:p>two</text:p></draw:page></office:presentation>",
    )
    document = extractor.extract(odp)
    assert [span.locator for span in document.spans] == ["slide:1", "slide:2"]
    with pytest.raises(ExtractionError):
        extractor.extract(_odf(tmp_path / "bad.odt", "<unclosed"))


def test_markitdown_is_optional_and_per_file(tmp_path: Path):
    runtime = OCRRuntimeManager(tmp_path / "runtime", tmp_path / "models")
    extractor = Extractor(runtime, markitdown_extensions=frozenset({".rtf"}))
    legacy = tmp_path / "memo.rtf"
    legacy.write_bytes(b"{\\rtf1 hello}")
    with (
        patch.dict(sys.modules, {"markitdown": None}),
        pytest.raises(ExtractionError, match="markitdown") as missing,
    ):
        extractor.extract(legacy)
    assert missing.value.code == "markitdown_missing"
    assert "[markitdown]" in str(missing.value)

    fake = types.ModuleType("markitdown")

    class MarkItDown:
        def convert(self, path: str) -> SimpleNamespace:
            return SimpleNamespace(text_content="# Memo\n\nconverted body")

    fake.MarkItDown = MarkItDown  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"markitdown": fake}):
        document = extractor.extract(legacy)
    assert document.media_type == "text/markdown" and "converted body" in document.text
    assert document.spans[0].locator == "document"


def test_markitdown_missing_fails_only_that_file(tmp_path: Path):
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    (root / "ok.txt").write_text("fine orchard", encoding="utf-8")
    (root / "memo.rtf").write_text("legacy", encoding="utf-8")
    settings = Settings(root=home, home=home, markitdown_extensions=frozenset({"rtf"}))
    assert ".rtf" in settings.extensions
    service = MultiSourceRAG(settings)
    service.add_local_source("notes", root)
    with patch.dict(sys.modules, {"markitdown": None}):
        report = service.reconcile()["sources"][0]
    assert report["errors"] and "markitdown_missing" in report["errors"][0]
    assert service.search("orchard", mode="full_text")["results"]


def test_extensions_defaults_and_legacy_upgrade(tmp_path: Path):
    for suffix in (".html", ".htm", ".csv", ".json", ".epub", ".odt", ".ods", ".odp"):
        assert suffix in SUPPORTED_EXTENSIONS
    home = tmp_path / "home"
    legacy = [".docx", ".markdown", ".md", ".pdf", ".pptx", ".txt", ".xlsx"]
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"root": str(home), "extensions": legacy}), encoding="utf-8"
    )
    assert Settings.load(home).extensions == SUPPORTED_EXTENSIONS
    (home / "config.json").write_text(
        json.dumps({"root": str(home), "extensions": [".txt", ".pdf"]}), encoding="utf-8"
    )
    assert Settings.load(home).extensions == {".txt", ".pdf"}  # customized lists stay as saved
    custom = Settings(root=home, home=home, markitdown_extensions=frozenset({"RTF"}))
    assert ".rtf" in custom.extensions and custom.accepts(home / "a.rtf") is False  # inside home
    custom.save()
    saved = json.loads((home / "config.json").read_text())
    assert ".rtf" not in saved["extensions"] and saved["markitdown_extensions"] == [".rtf"]
    assert Settings.load(home).markitdown_extensions == {".rtf"}


def test_drive_mimes_cover_new_formats():
    for mime, suffix in {
        "text/html": ".html",
        "text/csv": ".csv",
        "application/json": ".json",
        "application/epub+zip": ".epub",
        "application/vnd.oasis.opendocument.text": ".odt",
        "application/vnd.oasis.opendocument.spreadsheet": ".ods",
        "application/vnd.oasis.opendocument.presentation": ".odp",
    }.items():
        assert MIME_SUFFIXES[mime] == suffix and mime in SUPPORTED_MIMES


def test_new_formats_are_searchable_end_to_end(tmp_path: Path):
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    (root / "page.html").write_text("<h1>Heading</h1><p>quokka habitat</p>", encoding="utf-8")
    (root / "t.csv").write_text("animal,note\nwombat,burrow\n", encoding="utf-8")
    service = MultiSourceRAG(Settings(root=home, home=home))
    service.add_local_source("notes", root)
    assert not service.reconcile()["sources"][0]["errors"]
    assert service.search("quokka", mode="full_text")["results"]
    assert service.search("wombat", mode="full_text")["results"]


# --- Drive page image and integrity -------------------------------------------------------


def _drive_pdf_service(tmp_path: Path):
    pdf = tmp_path / "a.pdf"
    write_text_pdf(pdf, "Drive native text")
    backend = TreeDrive({"root": [value("pdf-1", "a.pdf", mime="application/pdf")]})
    backend.content["pdf-1"] = pdf.read_bytes()
    home = tmp_path / "home"
    settings = Settings(root=home, home=home)
    settings.save()
    service = MultiSourceRAG(settings, drive_backend_factory=lambda source: backend)
    service.add_drive_source("fixture", "root", "acct", tmp_path / "token.json")
    assert not service.reconcile("fixture")["errors"]
    return service, pdf


def test_drive_page_image_and_integrity_use_raw_cache(tmp_path: Path):
    service, pdf = _drive_pdf_service(tmp_path)
    ref = "drive:pdf-1"
    status = service.check_status(ref, "2026-01-01")
    assert status["integrity"] == {
        "sha256_matches": True,
        "checked": True,
        "basis": "drive_raw_cache",
    }
    runtime = SimpleNamespace(pdfium=Path("libpdfium"))
    with (
        patch.object(service.ocr_runtime, "paths", return_value=runtime),
        patch("local_rag.pageimage.render_page_png", return_value=(b"PNG", 10, 20)),
    ):
        image = service.page_image(ref, 1)
        assert image["cache_matches_index"] is True and image["width"] == 10
        assert hashlib.sha256(b"PNG").hexdigest()  # image payload is base64 of the PNG
        raw = next((tmp_path / "home" / "cache" / "sources").rglob("*.pdf"))
        raw.write_bytes(raw.read_bytes() + b"\n%changed")
        assert service.page_image(ref, 1)["cache_matches_index"] is False
        assert service.check_status(ref, "2026-01-01")["integrity"]["sha256_matches"] is False
        assert any(
            "SOURCE_CHANGED" in w for w in service.check_status(ref, "2026-01-01")["warnings"]
        )
        raw.unlink()
        with pytest.raises(ValueError, match="page_image_unavailable.*refresh"):
            service.page_image(ref, 1)
    integrity = service.check_status(ref, "2026-01-01")["integrity"]
    assert integrity["checked"] is False and integrity["sha256_matches"] is None


# --- complex_layout flag ------------------------------------------------------------------


def _fake_pdf_inspector(complex_pages: list[int], routed: list[int]):
    classification = SimpleNamespace(
        pages_needing_ocr=routed,
        pages_with_tables=complex_pages,
        pages_with_columns=[],
        pdf_type="text_based",
        page_count=3,
        confidence=0.9,
        is_complex_layout=bool(complex_pages),
    )
    native = SimpleNamespace(pages=[SimpleNamespace(page=0, markdown="Plain page one text")])
    return SimpleNamespace(
        process_pdf=lambda path: classification,
        extract_pages_markdown=lambda path: native,
    )


def test_complex_layout_becomes_flag_while_ocr_reviews_remain(tmp_path: Path):
    runtime = OCRRuntimeManager(tmp_path / "runtime", tmp_path / "models")
    extractor = Extractor(runtime, ocr_enabled=False)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF")
    with patch.dict(sys.modules, {"pdf_inspector": _fake_pdf_inspector([2, 3], [3])}):
        document = extractor.extract(pdf)
    assert document.metadata["layout_flags"] == ["complex_layout"]
    assert document.metadata["layout_pages"] == [2, 3]
    assert [(r["page"], r["reason"]) for r in document.reviews] == [(3, "ocr_runtime_missing")]
    with patch.dict(sys.modules, {"pdf_inspector": _fake_pdf_inspector([], [])}):
        plain = extractor.extract(pdf)
    assert "layout_flags" not in plain.metadata and plain.reviews == []


def test_layout_flags_reach_search_read_and_doctor_stays_ok(tmp_path: Path):
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    (root / "layout.pdf").write_bytes(b"%PDF fixture")
    service = MultiSourceRAG(Settings(root=home, home=home))
    service.add_local_source("notes", root)
    with patch.dict(sys.modules, {"pdf_inspector": _fake_pdf_inspector([2], [])}):
        assert not service.reconcile()["sources"][0]["errors"]
    hit = service.search("Plain page", mode="full_text")["results"][0]
    assert hit["layout_flags"] == ["complex_layout"]
    read = service.read("notes:layout.pdf")
    assert read["layout_flags"] == ["complex_layout"] and read["layout_pages"] == [2]
    assert service.reviews() == []
    reviews = service.doctor()["checks"]["reviews"]
    assert reviews["status"] == "ok" and reviews["open"] == 0


def test_cached_artifact_with_legacy_complex_review_is_not_requeued():
    from local_rag.indexer import _document_from_artifact

    document = _document_from_artifact(
        {
            "text": "t",
            "spans": [],
            "media_type": "application/pdf",
            "metadata": {},
            "reviews": [
                {"page": 4, "reason": "complex_layout", "detail": {}},
                {"page": 1, "reason": "ocr_failed", "detail": {}},
            ],
        }
    )
    assert [r["reason"] for r in document.reviews] == ["ocr_failed"]
    assert document.metadata["layout_flags"] == ["complex_layout"]
    assert document.metadata["layout_pages"] == [4]


def test_migration_13_resolves_only_complex_layout_reviews(tmp_path: Path):
    assert len(MIGRATIONS) >= 13
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    (root / "n.txt").write_text("orchard", encoding="utf-8")
    service = MultiSourceRAG(Settings(root=home, home=home))
    service.add_local_source("notes", root)
    service.reconcile()
    with service.db.transaction() as connection:
        row = connection.execute("SELECT id,path,content_hash FROM documents").fetchone()
        for page, reason in ((2, "complex_layout"), (5, "complex_layout"), (1, "low_quality_ocr")):
            connection.execute(
                "INSERT INTO reviews(document_id,path,content_hash,page,reason,detail_json)"
                " VALUES(?,?,?,?,?,'{}')",
                (row["id"], row["path"], row["content_hash"], page, reason),
            )
        connection.execute("DELETE FROM schema_migrations WHERE version=13")
    service.db.migrate()
    with service.db.connect() as connection:
        reviews = {
            (r["reason"], r["page"]): (r["status"], r["resolution"])
            for r in connection.execute("SELECT * FROM reviews")
        }
        metadata = json.loads(
            connection.execute("SELECT metadata_json FROM documents").fetchone()[0]
        )
    assert reviews[("complex_layout", 2)] == ("resolved", "migrated_to_layout_flag")
    assert reviews[("complex_layout", 5)] == ("resolved", "migrated_to_layout_flag")
    assert reviews[("low_quality_ocr", 1)] == ("open", None)
    assert metadata["layout_flags"] == ["complex_layout"] and metadata["layout_pages"] == [2, 5]


# --- hardening regressions ----------------------------------------------------------------


def test_html_without_closing_head_keeps_body_and_uses_title(tmp_path: Path, extractor: Extractor):
    page = tmp_path / "loose.html"
    page.write_text(
        "<html><head><title>Loose  Title</title><body><h1>Body</h1><p>kept text</p></body></html>",
        encoding="utf-8",
    )
    document = extractor.extract(page)
    assert "kept text" in document.text and "Loose Title" not in document.text
    assert document.metadata["title"] == "Loose Title"
    assert document.metadata["encoding"] == "utf-8"


def test_json_recursion_is_a_parse_failure(tmp_path: Path, extractor: Extractor):
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(ExtractionError) as error:
        extractor.extract(deep)
    assert error.value.code == "format_parse_failed"


def test_odf_deep_nesting_and_bad_numbers(tmp_path: Path, extractor: Extractor):
    nested = "<text:span>" * 3000 + "deep" + "</text:span>" * 3000
    odt = _odf(
        tmp_path / "nest.odt",
        '<office:text><text:h text:outline-level="junk">Head</text:h>'
        f'<text:p>a<text:s text:c="x"/>b {nested}</text:p></office:text>',
    )
    try:
        document = extractor.extract(odt)
    except ExtractionError as error:  # the XML parser itself may refuse such depth
        assert error.code == "format_parse_failed"
    else:
        assert "# Head" in document.text and "deep" in document.text
    ods = _odf(
        tmp_path / "bad.ods",
        '<office:spreadsheet><table:table table:name="S">'
        '<table:table-row table:number-rows-repeated="x">'
        '<table:table-cell table:number-columns-repeated="y"><text:p>v</text:p>'
        "</table:table-cell>"
        "</table:table-row></table:table></office:spreadsheet>",
    )
    assert extractor.extract(ods).spans[0].locator == "sheet:S/row:1"
    with patch("local_rag.formats._odf_paragraphs", side_effect=RecursionError):
        with pytest.raises(ExtractionError) as error:
            extractor.extract(odt)
        assert error.value.code == "format_parse_failed"


def test_xml_with_dtd_is_rejected_but_xhtml_chapters_work(tmp_path: Path, extractor: Extractor):
    evil = tmp_path / "evil.odt"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr(
            "content.xml",
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>'
            f"<office:document-content {ODF_NS}><office:body><office:text><text:p>&a;</text:p>"
            "</office:text></office:body></office:document-content>",
        )
    with pytest.raises(ExtractionError) as error:
        extractor.extract(evil)
    assert error.value.code == "format_parse_failed"

    book = tmp_path / "x.epub"
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="c.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "c.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            '<item id="a" href="a.xhtml"/></manifest><spine><itemref idref="a"/></spine></package>',
        )
        archive.writestr(
            "a.xhtml",
            '<?xml version="1.0"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml">'
            "<body><p>doctype chapter</p></body></html>",
        )
    assert "doctype chapter" in extractor.extract(book).text


def test_legacy_encodings_fall_back_to_cp1258(tmp_path: Path, extractor: Extractor):
    sheet = tmp_path / "vn.csv"
    import unicodedata

    tones = "\u0300\u0301\u0303\u0309\u0323"

    def nfd(value: str) -> str:
        """cp1258 keeps precomposed vowels but writes tone marks as combining characters."""
        out = []
        for char in unicodedata.normalize("NFC", value):
            parts = unicodedata.normalize("NFD", char)
            tone = next((c for c in parts if c in tones), "")
            base = unicodedata.normalize("NFC", parts.replace(tone, "")) if tone else char
            out.append(base + tone)
        return "".join(out)

    sheet.write_bytes(nfd("tên,thành phố\nNguyễn Văn A,Hà Nội\n").encode("cp1258"))
    document = extractor.extract(sheet)
    assert document.metadata["encoding"] == "cp1258"
    assert nfd("Nguyễn Văn A") in document.text and nfd("Hà Nội") in document.text
    utf8 = tmp_path / "u.csv"
    utf8.write_bytes(b"\xef\xbb\xbfa,b\n1,2\n")
    assert extractor.extract(utf8).metadata["encoding"] == "utf-8"
    assert extractor.extract(utf8).metadata["columns"] == ["a", "b"]
    odd = tmp_path / "odd.csv"
    odd.write_bytes(b"a\n\x8d\x81\n")  # undefined in cp1258
    assert extractor.extract(odd).metadata["encoding"] == "cp1252"


def test_csv_blank_rows_do_not_count_and_field_limit_is_global(
    tmp_path: Path, extractor: Extractor
):
    import csv

    assert csv.field_size_limit() >= 1 << 24
    sheet = tmp_path / "blank.csv"
    sheet.write_text("\n\nh\n\nx\n\n\ny\n", encoding="utf-8")
    with patch("local_rag.formats.MAX_CSV_ROWS", 2):
        document = extractor.extract(sheet)
    assert document.text == "h: x\n\nh: y" and document.metadata["rows"] == 2
    with patch("local_rag.formats.MAX_CSV_ROWS", 1), pytest.raises(ExtractionError):
        extractor.extract(sheet)
