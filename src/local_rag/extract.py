from __future__ import annotations

import datetime as dt
import mimetypes
import re
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import formats
from .builder import Builder as _Builder
from .extraction_errors import ExtractionError
from .models import ExtractedDocument
from .ocr_runtime import OCRRuntimeManager
from .xlsx_compat import (
    STYLES_PART,
    color_failure,
    copy_archive,
    inspect_archive,
    repair_styles,
    stylesheet_failure,
)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
IMAGE_MAX_SIDE = 4096
IMAGE_PDF_DPI = 200  # pixel density of the one-page PDF a photo is wrapped in for OCR


class Extractor:
    def __init__(
        self,
        ocr_runtime: OCRRuntimeManager,
        *,
        ocr_enabled: bool = True,
        ocr_offline: bool = False,
        docx_document_order: bool = False,
        cache_dir: Path | None = None,
        markitdown_extensions: frozenset[str] = frozenset(),
    ):
        self.markitdown_extensions = markitdown_extensions
        self.cache_dir = cache_dir
        self.ocr_runtime = ocr_runtime
        self.ocr_enabled = ocr_enabled
        self.ocr_offline = ocr_offline
        self.docx_document_order = docx_document_order

    def extract(self, path: Path) -> ExtractedDocument:
        suffix = path.suffix.lower()
        if suffix in self.markitdown_extensions:
            return formats.extract_markitdown(path)
        if suffix in {".htm", ".html"}:
            return formats.extract_html(path)
        if suffix == ".csv":
            return formats.extract_csv(path)
        if suffix == ".json":
            return formats.extract_json(path)
        if suffix == ".epub":
            return formats.extract_epub(path)
        if suffix in {".odt", ".ods", ".odp"}:
            return formats.extract_odf(path)
        if suffix in {".txt", ".md", ".markdown"}:
            return _text(path, markdown=suffix != ".txt")
        if suffix == ".docx":
            return _docx(path, self.docx_document_order)
        if suffix == ".xlsx":
            return _xlsx(path, self.cache_dir)
        if suffix == ".pptx":
            return _pptx(path)
        if suffix == ".pdf":
            return self._pdf(path)
        if suffix in IMAGE_SUFFIXES:
            return self._image(path)
        raise ValueError(f"unsupported file type: {suffix}")

    def _ocr(
        self, run: Callable[[str | None], Any], routed: list[int]
    ) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
        """Run local OCR for ``routed`` 1-based pages; return page texts and review items."""
        pages: dict[int, dict[str, Any]] = {}
        reviews: list[dict[str, Any]] = []
        if not (self.ocr_enabled and self.ocr_runtime.configure()):
            reviews.extend(
                {
                    "page": page,
                    "reason": "ocr_runtime_missing",
                    "detail": {
                        "action": "run local-rag-mcp setup --full, then reindex --reextract"
                    },
                }
                for page in routed
            )
            return pages, reviews
        try:
            model_directory = self.ocr_runtime.resolved_model_dir()
            result = run(str(model_directory) if model_directory else None)
            for ocr_page in result.pages:
                number = int(ocr_page.page_number)
                markdown = (ocr_page.markdown or "").strip()
                provenance = ocr_page.provenance
                if markdown:
                    pages[number] = {
                        "text": markdown,
                        "source": provenance.source,
                        "confidence": provenance.ocr_confidence,
                    }
                if not markdown or provenance.hosted_recommended:
                    reviews.append(
                        {
                            "page": number,
                            "reason": "low_quality_ocr" if markdown else "blank_or_failed_ocr",
                            "detail": {"warnings": list(provenance.warnings)},
                        }
                    )
        except Exception as exc:
            reviews.extend(
                {"page": page, "reason": "ocr_failed", "detail": {"error": str(exc)}}
                for page in routed
            )
        return pages, reviews

    def _image(self, path: Path) -> ExtractedDocument:
        """OCR a photo or scan as one page: orient it, wrap it in a PDF, reuse the PDF OCR path."""
        import io

        import pdf_inspector
        from PIL import Image, ImageOps

        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
        except Exception as exc:
            raise ExtractionError("image_unreadable") from exc
        original = image.size
        if max(original) > IMAGE_MAX_SIDE:
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buffer = io.BytesIO()
        image.save(buffer, "PDF", resolution=IMAGE_PDF_DPI)
        pdf = buffer.getvalue()
        pages, reviews = self._ocr(
            lambda model: pdf_inspector.process_pdf_with_ocr_bytes(
                pdf, page_numbers=[1], model_directory=model, offline=self.ocr_offline
            ),
            [1],
        )
        builder = _Builder()
        for number, page_data in sorted(pages.items()):
            builder.add(
                page_data["text"],
                "pdf_page",
                f"page:{number}",
                {key: value for key, value in page_data.items() if key != "text"},
            )
        metadata = {
            "page_count": 1,
            "pages_routed_to_ocr": [1],
            "image_width": original[0],
            "image_height": original[1],
        }
        return ExtractedDocument(builder.text, builder.spans, _media(path), metadata, reviews)

    def _pdf(self, path: Path) -> ExtractedDocument:
        import pdf_inspector

        classification = pdf_inspector.process_pdf(str(path))
        native = pdf_inspector.extract_pages_markdown(str(path))
        routed = list(classification.pages_needing_ocr)
        pages: dict[int, dict[str, Any]] = {}
        for native_page in native.pages:
            number = int(native_page.page) + 1
            markdown = (native_page.markdown or "").strip()
            if markdown:
                pages[number] = {"text": markdown, "source": "native"}
        reviews: list[dict[str, Any]] = []
        if routed:
            ocr_pages, reviews = self._ocr(
                lambda model: pdf_inspector.process_pdf_with_ocr(
                    str(path), page_numbers=routed, model_directory=model, offline=self.ocr_offline
                ),
                routed,
            )
            pages.update(ocr_pages)
        complex_pages = sorted(
            set(classification.pages_with_tables) | set(classification.pages_with_columns)
        )
        builder = _Builder()
        for number, page_data in sorted(pages.items()):
            builder.add(
                page_data["text"],
                "pdf_page",
                f"page:{number}",
                {key: value for key, value in page_data.items() if key != "text"},
            )
        metadata = {
            "pdf_type": classification.pdf_type,
            "page_count": classification.page_count,
            "classification_confidence": classification.confidence,
            "pages_routed_to_ocr": routed,
            "complex_layout": bool(classification.is_complex_layout),
        }
        if complex_pages:
            # Layout is informational (tables/columns), not a work item: flag it, do not queue it.
            metadata["layout_flags"] = ["complex_layout"]
            metadata["layout_pages"] = complex_pages
        return ExtractedDocument(builder.text, builder.spans, "application/pdf", metadata, reviews)


def _text(path: Path, markdown: bool) -> ExtractedDocument:
    text = path.read_text(encoding="utf-8", errors="replace")
    builder = _Builder()
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            kind = "markdown_line" if markdown else "text_line"
            builder.add(line, kind, f"line:{number}")
    media = "text/markdown" if markdown else "text/plain"
    return ExtractedDocument(builder.text, builder.spans, media)


def _docx(path: Path, document_order: bool = False) -> ExtractedDocument:
    import re

    from docx import Document
    from docx.text.paragraph import Paragraph

    document, builder = Document(str(path)), _Builder()
    headings: dict[int, str] = {}
    blocks: list[tuple[str, Any, int, list[str]]] = []
    paragraph_index = table_index = 0
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            paragraph_index += 1
            style = block.style.name if block.style is not None else ""
            match = re.fullmatch(r"Heading (\d+)", style or "", re.IGNORECASE)
            if match:
                level = int(match[1])
                headings = {key: value for key, value in headings.items() if key < level}
                headings[level] = block.text.strip()
            blocks.append(("paragraph", block, paragraph_index, list(headings.values())))
        else:
            table_index += 1
            blocks.append(("table", block, table_index, list(headings.values())))
    if not document_order:
        blocks.sort(key=lambda item: item[0] != "paragraph")
    for kind, block, index, heading_path in blocks:
        metadata = {"heading_path": heading_path}
        if kind == "paragraph":
            builder.add(block.text, "docx_paragraph", f"paragraph:{index}", metadata)
        else:
            for row_index, row in enumerate(block.rows, 1):
                for column_index, cell in enumerate(row.cells, 1):
                    builder.add(
                        cell.text,
                        "docx_cell",
                        f"table:{index}/row:{row_index}/column:{column_index}",
                        metadata,
                    )
    return ExtractedDocument(builder.text, builder.spans, _media(path))


def _xlsx(path: Path, cache_dir: Path | None = None) -> ExtractedDocument:
    from openpyxl import load_workbook

    builder = _Builder()
    repairs: list[dict[str, Any]] = []
    try:
        styles = inspect_archive(path)
        with ExitStack() as stack:
            try:
                workbook = load_workbook(path, read_only=True, data_only=True)
            except Exception as original:
                if not color_failure(original) or styles is None:
                    raise
                try:
                    repaired, count = repair_styles(styles)
                    if not count:
                        raise original
                    if cache_dir is not None:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                    temporary = stack.enter_context(
                        tempfile.TemporaryDirectory(prefix="xlsx-", dir=cache_dir)
                    )
                    copy = Path(temporary) / "workbook.xlsx"
                    copy_archive(path, copy, repaired)
                    workbook = load_workbook(copy, read_only=True, data_only=True)
                except ExtractionError as exc:
                    raise exc from original
                except Exception as exc:
                    raise ExtractionError("xlsx_stylesheet_unreadable") from exc
                repairs = [{"kind": "xlsx_rgb_hash_prefix", "part": STYLES_PART, "count": count}]
            stack.callback(workbook.close)
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value is not None:
                            builder.add(
                                _cell_text(cell.value, getattr(cell, "number_format", None)),
                                "xlsx_cell",
                                f"sheet:{sheet.title}/cell:{cell.coordinate}",
                            )
            metadata: dict[str, Any] = {"sheets": workbook.sheetnames}
            if repairs:
                metadata["compatibility_repairs"] = repairs
            return ExtractedDocument(builder.text, builder.spans, _media(path), metadata)
    except ExtractionError:
        raise
    except Exception as exc:
        code = (
            "xlsx_stylesheet_unreadable"
            if repairs or stylesheet_failure(exc)
            else "xlsx_parse_failed"
        )
        raise ExtractionError(code) from exc


_QUOTED = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')


def _cell_text(value: Any, number_format: str | None) -> str:
    """Render a cell roughly as a spreadsheet displays it, so amounts and dates are searchable.

    Strings are unchanged. Dates follow a day-first or ISO order taken from the number format and
    drop a midnight time; numbers drop a spurious ``.0``, keep the format's decimal places, and
    use thousands separators or a percent sign when the format asks for them. Currency or other
    literal text in the format is not reproduced.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    pattern = _QUOTED.sub("", (number_format or "General").split(";")[0])
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return _date_text(value.date(), pattern)
        return f"{_date_text(value.date(), pattern)} {_time_text(value.time())}"
    if isinstance(value, dt.date):
        return _date_text(value, pattern)
    if isinstance(value, dt.time):
        return _time_text(value)
    if isinstance(value, int | float):
        return _number_text(float(value) if isinstance(value, float) else value, pattern)
    return str(value)


def _date_text(value: dt.date, pattern: str) -> str:
    if pattern.lower().lstrip().startswith("d"):
        return value.strftime("%d/%m/%Y")
    return value.isoformat()


def _time_text(value: dt.time) -> str:
    return value.strftime("%H:%M:%S" if value.second else "%H:%M")


def _number_text(value: int | float, pattern: str) -> str:
    percent = "%" in pattern
    number = value * 100 if percent else value
    match = re.search(r"\.([0#?]+)", pattern)
    decimals = len(match.group(1)) if match else None
    grouped = bool(re.search(r"[#0],[#0]", pattern))
    if decimals is None:
        if isinstance(number, int) or (
            isinstance(number, float) and number.is_integer() and abs(number) < 1e15
        ):
            text = f"{int(number):,}" if grouped else str(int(number))
        else:
            text = f"{number:,.10g}" if grouped else f"{number:.10g}"
    else:
        text = f"{number:,.{decimals}f}" if grouped else f"{number:.{decimals}f}"
    return f"{text}%" if percent else text


def _pptx(path: Path) -> ExtractedDocument:
    from pptx import Presentation

    presentation, builder = Presentation(str(path)), _Builder()
    for slide_index, slide in enumerate(presentation.slides, 1):
        for shape_index, shape in enumerate(slide.shapes, 1):
            text = getattr(shape, "text", "")
            builder.add(text, "pptx_shape", f"slide:{slide_index}/shape:{shape_index}")
    return ExtractedDocument(
        builder.text, builder.spans, _media(path), {"slides": len(presentation.slides)}
    )


def _media(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
