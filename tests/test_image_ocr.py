from __future__ import annotations

import base64
import io
import json
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from local_rag.config import DOCUMENT_EXTENSIONS, SUPPORTED_EXTENSIONS, Settings
from local_rag.drive import MIME_SUFFIXES, SUPPORTED_MIMES
from local_rag.extract import IMAGE_MAX_SIDE, IMAGE_PDF_DPI, Extractor
from local_rag.extraction_errors import ExtractionError
from local_rag.ocr_runtime import OCRRuntimeManager
from local_rag.service import MultiSourceRAG


def photo(path: Path, size=(40, 20), orientation: int | None = None) -> Path:
    image = Image.new("RGB", size, "white")
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    image.save(path, exif=exif)
    return path


class FakeOCR:
    """Stand-in for pdf_inspector's byte OCR entry point (no model, no network)."""

    def __init__(self, markdown="Phiếu chi 620.000", hosted=False):
        self.markdown, self.hosted, self.calls = markdown, hosted, []

    def process_pdf_with_ocr_bytes(self, data, **options):
        self.calls.append((data, options))
        provenance = types.SimpleNamespace(
            source="ocr", ocr_confidence=0.91, hosted_recommended=self.hosted, warnings=["w"]
        )
        page = types.SimpleNamespace(page_number=1, markdown=self.markdown, provenance=provenance)
        return types.SimpleNamespace(pages=[page])

    def module(self):
        return types.SimpleNamespace(process_pdf_with_ocr_bytes=self.process_pdf_with_ocr_bytes)


def extractor(tmp_path: Path, enabled=True) -> Extractor:
    runtime = OCRRuntimeManager(tmp_path / "runtime", tmp_path / "models")
    return Extractor(runtime, ocr_enabled=enabled, ocr_offline=True)


def run(extract: Extractor, path: Path, fake: FakeOCR):
    with (
        patch.object(extract.ocr_runtime, "configure", return_value=True),
        patch.object(extract.ocr_runtime, "resolved_model_dir", return_value=Path("/models")),
        patch.dict(sys.modules, {"pdf_inspector": fake.module()}),
    ):
        return extract.extract(path)


def test_image_is_ocred_as_one_upright_page(tmp_path):
    fake = FakeOCR()
    # EXIF orientation 6: stored 40x20, displayed upright as 20x40.
    document = run(extractor(tmp_path), photo(tmp_path / "receipt.jpg", orientation=6), fake)
    assert document.text == "Phiếu chi 620.000"
    assert document.media_type == "image/jpeg"
    assert [(s.kind, s.locator) for s in document.spans] == [("pdf_page", "page:1")]
    assert document.spans[0].metadata == {"source": "ocr", "confidence": 0.91}
    assert document.metadata["image_width"] == 20 and document.metadata["image_height"] == 40
    assert document.reviews == []
    data, options = fake.calls[0]
    assert data.startswith(b"%PDF") and options["page_numbers"] == [1]
    assert options["offline"] is True and options["model_directory"] == "/models"


def test_large_image_is_bounded_before_ocr(tmp_path):
    fake = FakeOCR()
    run(extractor(tmp_path), photo(tmp_path / "wide.png", size=(IMAGE_MAX_SIDE * 2, 10)), fake)
    box = re.search(rb"/MediaBox \[\s*0 0 ([\d.]+)", fake.calls[0][0])
    assert box is not None
    assert float(box.group(1)) == pytest.approx(IMAGE_MAX_SIDE * 72 / IMAGE_PDF_DPI, abs=0.01)


def test_low_quality_and_missing_runtime_go_to_review(tmp_path):
    image = photo(tmp_path / "scan.png")
    low = run(extractor(tmp_path), image, FakeOCR(hosted=True))
    assert [(r["page"], r["reason"]) for r in low.reviews] == [(1, "low_quality_ocr")]
    blank = run(extractor(tmp_path), image, FakeOCR(markdown="  "))
    assert blank.text == "" and blank.reviews[0]["reason"] == "blank_or_failed_ocr"
    off = extractor(tmp_path, enabled=False).extract(image)
    assert off.text == "" and off.reviews[0]["reason"] == "ocr_runtime_missing"


def test_undecodable_image_is_a_content_free_error(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    with pytest.raises(ExtractionError) as error:
        extractor(tmp_path).extract(broken)
    assert error.value.code == "image_unreadable"


def test_image_formats_are_registered():
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        assert suffix in SUPPORTED_EXTENSIONS
    for mime, suffix in {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.items():
        assert MIME_SUFFIXES[mime] == suffix and mime in SUPPORTED_MIMES


def test_home_saved_with_previous_default_adopts_images(tmp_path):
    settings = Settings(root=tmp_path, home=tmp_path / "home", extensions=DOCUMENT_EXTENSIONS)
    settings.save()
    assert Settings.load(tmp_path / "home").extensions == SUPPORTED_EXTENSIONS
    custom = Settings(root=tmp_path, home=tmp_path / "custom", extensions=frozenset({".pdf"}))
    custom.save()
    assert Settings.load(tmp_path / "custom").extensions == frozenset({".pdf"})


def test_local_image_is_searchable_cited_and_viewable(tmp_path):
    root, home = tmp_path / "docs", tmp_path / "home"
    root.mkdir()
    photo(root / "phieu.png", size=(30, 60))
    settings = Settings(root=home, home=home, ocr_mode="no-ocr")
    settings.save()
    service = MultiSourceRAG(settings)
    service.extractor.ocr_enabled = True
    service.add_local_source("phieu", root)
    fake = FakeOCR("Phiếu chi tiền nhà 5.000.000")
    with (
        patch.object(service.extractor.ocr_runtime, "configure", return_value=True),
        patch.object(service.extractor.ocr_runtime, "resolved_model_dir", return_value=None),
        patch.dict(sys.modules, {"pdf_inspector": fake.module()}),
    ):
        report = service.reconcile()
    assert not report["sources"][0]["errors"]
    result = service.search("tiền nhà", source="phieu", mode="full_text")["results"][0]
    assert result["path"] == "phieu.png" and result["page_start"] == 1
    assert result["ocr"] is True and result["ocr_notice"]
    image = service.page_image("phieu.png", 1, source="phieu")
    picture = Image.open(io.BytesIO(base64.b64decode(image["image_base64"])))
    assert picture.size == (30, 60) and image["source_hash_matches_index"] is True
    with pytest.raises(ValueError, match="single page"):
        service.page_image("phieu.png", 2, source="phieu")
    stored = json.loads(service.db.resolve_document("phieu.png", "phieu")["metadata_json"])
    assert stored["image_width"] == 30
