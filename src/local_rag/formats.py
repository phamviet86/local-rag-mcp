"""Standard-library extractors for HTML, CSV, JSON, EPUB and OpenDocument files.

markitdown is an optional converter for operator-listed extensions; it is imported lazily and a
missing library becomes a per-file extraction error, never a source-wide failure.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import posixpath
import re
import zipfile
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from .builder import Builder
from .extraction_errors import ExtractionError
from .models import ExtractedDocument

MAX_TEXT_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRY_BYTES = 30 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5000
MAX_JSON_LEAVES = 200_000
MAX_CSV_ROWS = 200_000
csv.field_size_limit(1 << 24)
MAX_CELL_REPEAT = 256

ODT_MEDIA = "application/vnd.oasis.opendocument.text"
ODS_MEDIA = "application/vnd.oasis.opendocument.spreadsheet"
ODP_MEDIA = "application/vnd.oasis.opendocument.presentation"

_SKIPPED = frozenset({"script", "style", "noscript", "template", "svg"})
_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_BLOCKS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "ul", "ol",
        "table", "tr", "pre", "blockquote", "figure", "figcaption", "dl", "dt", "dd", "form",
        "caption", "fieldset", "details", "summary",
    }
)  # fmt: skip


def read_limited(path: Path, limit: int | None = None) -> bytes:
    if path.stat().st_size > (MAX_TEXT_BYTES if limit is None else limit):
        raise ExtractionError("format_too_large")
    return path.read_bytes()


class _HTMLBlocks(HTMLParser):
    """Collect visible text as Markdown-ish blocks (headings keep their ``#`` prefix)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._current: list[str] = []
        self._prefix = ""
        self._skip = 0
        self._in_title = False
        self.title = ""

    def _flush(self) -> None:
        text = " ".join("".join(self._current).split()).strip(" |")
        if text:
            self.blocks.append(self._prefix + text)
        self._current = []
        self._prefix = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADINGS:
            self._flush()
            self._prefix = "#" * _HEADINGS[tag] + " "
        elif tag == "li":
            self._flush()
            self._prefix = "- "
        elif tag in _BLOCKS:
            self._flush()
        elif tag in {"td", "th"}:
            self._current.append(" | ")
        elif tag == "br":
            self._current.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _SKIPPED:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if not self._skip and (tag in _HEADINGS or tag in _BLOCKS or tag == "li"):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self._current.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def html_parse(text: str) -> tuple[list[str], str]:
    parser = _HTMLBlocks()
    parser.feed(text)
    parser.close()
    return parser.blocks, " ".join(parser.title.split())


def html_blocks(text: str) -> list[str]:
    return html_parse(text)[0]


def _decode(data: bytes) -> tuple[str, str]:
    """Strict UTF-8 first, then cp1258 (Vietnamese), then lossy cp1252; returns (text, encoding)."""
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1258"), "cp1258"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def extract_html(path: Path) -> ExtractedDocument:
    builder = Builder()
    text, encoding = _decode(read_limited(path))
    blocks, title = html_parse(text)
    for number, block in enumerate(blocks, 1):
        builder.add(block, "html_block", f"block:{number}")
    metadata: dict[str, Any] = {"encoding": encoding}
    if title:
        metadata["title"] = title
    return ExtractedDocument(builder.text, builder.spans, "text/markdown", metadata)


def extract_csv(path: Path) -> ExtractedDocument:
    text, encoding = _decode(read_limited(path))
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    builder = Builder()
    header: list[str] = []
    seen_header = False
    rows = 0
    try:
        for number, row in enumerate(csv.reader(io.StringIO(text, newline=""), dialect), 1):
            if not any(cell.strip() for cell in row):
                continue  # blank lines neither count toward the limit nor become rows
            if not seen_header:
                header, seen_header = [cell.strip() for cell in row], True
                continue
            if rows >= MAX_CSV_ROWS:
                raise ExtractionError("format_too_large")
            pairs = []
            for index, cell in enumerate(row):
                if cell.strip():
                    name = (
                        header[index]
                        if index < len(header) and header[index]
                        else f"col{index + 1}"
                    )
                    pairs.append(f"{name}: {cell.strip()}")
            rows += 1
            builder.add("; ".join(pairs), "csv_row", f"row:{number}")
    except csv.Error as exc:
        raise ExtractionError("format_parse_failed") from exc
    return ExtractedDocument(
        builder.text,
        builder.spans,
        "text/csv",
        {"columns": header, "rows": rows, "encoding": encoding},
    )


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _json_key(path: str, key: str) -> str:
    return f"{path}.{key}" if _IDENTIFIER.fullmatch(key) else f"{path}[{json.dumps(key)}]"


def extract_json(path: Path) -> ExtractedDocument:
    try:
        text, encoding = _decode(read_limited(path))
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ExtractionError):
            raise
        raise ExtractionError("format_parse_failed") from exc
    builder = Builder()
    stack: list[tuple[str, Any]] = [("$", data)]
    leaves = 0
    while stack:
        location, value = stack.pop()
        if isinstance(value, dict):
            stack.extend(
                (_json_key(location, str(key)), item) for key, item in reversed(value.items())
            )
        elif isinstance(value, list):
            stack.extend(
                (f"{location}[{index}]", item) for index, item in reversed(list(enumerate(value)))
            )
        else:
            leaves += 1
            if leaves > MAX_JSON_LEAVES:
                raise ExtractionError("format_too_large")
            rendered = value if isinstance(value, str) else json.dumps(value)
            label = location[2:] if location.startswith("$.") else location
            builder.add(f"{label}: {rendered}", "json_value", location)
    return ExtractedDocument(
        builder.text, builder.spans, "application/json", {"values": leaves, "encoding": encoding}
    )


def _open_archive(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ExtractionError("format_parse_failed") from exc
    infos = archive.infolist()
    if (
        len(infos) > MAX_ARCHIVE_ENTRIES
        or sum(i.file_size for i in infos) > MAX_ARCHIVE_TOTAL_BYTES
    ):
        archive.close()
        raise ExtractionError("format_too_large")
    return archive


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ExtractionError("format_parse_failed") from exc
    if info.file_size > MAX_ARCHIVE_ENTRY_BYTES:
        raise ExtractionError("format_too_large")
    return archive.read(info)


def _xml(data: bytes) -> ElementTree.Element:
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:  # package/content XML never needs a DTD
        raise ExtractionError("format_parse_failed")
    try:
        return ElementTree.fromstring(data)
    except (ElementTree.ParseError, RecursionError) as exc:
        raise ExtractionError("format_parse_failed") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _guarded(function: Callable[[Path], ExtractedDocument]) -> Callable[[Path], ExtractedDocument]:
    @functools.wraps(function)
    def wrapper(path: Path) -> ExtractedDocument:
        try:
            return function(path)
        except ExtractionError:
            raise
        except (RecursionError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            raise ExtractionError("format_parse_failed") from exc

    return wrapper


@_guarded
def extract_epub(path: Path) -> ExtractedDocument:
    builder = Builder()
    metadata: dict[str, Any] = {}
    with _open_archive(path) as archive:
        container = _xml(_member(archive, "META-INF/container.xml"))
        rootfile = next(
            (el.get("full-path") for el in container.iter() if _local(el.tag) == "rootfile"), None
        )
        if not rootfile:
            raise ExtractionError("format_parse_failed")
        package = _xml(_member(archive, rootfile))
        base = posixpath.dirname(rootfile)
        manifest: dict[str, str] = {}
        spine: list[str] = []
        for element in package.iter():
            name = _local(element.tag)
            if name == "item" and element.get("id") and element.get("href"):
                manifest[str(element.get("id"))] = str(element.get("href"))
            elif name == "itemref" and element.get("idref"):
                spine.append(str(element.get("idref")))
            elif name == "title" and element.text and "epub_title" not in metadata:
                metadata["epub_title"] = element.text.strip()
        chapter = 0
        for idref in spine:
            href = manifest.get(idref)
            if href is None:
                continue
            member = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
            if member.startswith("..") or member not in archive.namelist():
                continue
            blocks = html_blocks(_decode(_member(archive, member))[0])
            if blocks:
                chapter += 1
                builder.add("\n\n".join(blocks), "epub_chapter", f"chapter:{chapter}")
    metadata["chapters"] = chapter
    return ExtractedDocument(builder.text, builder.spans, "text/markdown", metadata)


def _safe_int(value: str | None, default: int = 1, low: int = 1, high: int = 1_000_000) -> int:
    try:
        return max(low, min(int(str(value)), high))
    except ValueError:
        return default


def _odf_text(element: ElementTree.Element) -> str:
    """Text of an ODF element, iteratively so deeply nested markup cannot recurse."""
    parts: list[str] = []
    stack: list[str | ElementTree.Element] = [element]
    root = element
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
            continue
        name = _local(item.tag)
        if item is not root:
            stack.append(item.tail or "")
        if name == "s":
            parts.append(" " * _safe_int(item.get(f"{_TEXT_NS}c"), 1, 1, 1000))
        elif name == "tab":
            parts.append("\t")
        elif name == "line-break":
            parts.append("\n")
        else:
            stack.extend(reversed(list(item)))
            stack.append(item.text or "")
    return "".join(parts)


_TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_DRAW_NS = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"


def _odf_paragraphs(root: ElementTree.Element) -> list[tuple[str, str]]:
    """(kind, text) for every paragraph or heading below ``root`` in document order."""
    found: list[tuple[str, str]] = []
    stack = [root]
    while stack:
        element = stack.pop()
        name = _local(element.tag)
        if name in {"p", "h"} and element.tag.startswith(_TEXT_NS):
            text = _odf_text(element).strip()
            if text:
                found.append((name, text if name == "p" else _heading(element, text)))
            continue
        stack.extend(reversed(list(element)))
    return found


def _heading(element: ElementTree.Element, text: str) -> str:
    level = _safe_int(element.get(f"{_TEXT_NS}outline-level"), 1, 1, 6)
    return "#" * max(1, min(level, 6)) + " " + text


@_guarded
def extract_odf(path: Path) -> ExtractedDocument:
    suffix = path.suffix.lower()
    builder = Builder()
    with _open_archive(path) as archive:
        content = _xml(_member(archive, "content.xml"))
    body = next((el for el in content if _local(el.tag) == "body"), None)
    if body is None:
        raise ExtractionError("format_parse_failed")
    if suffix == ".odt":
        for number, (_, text) in enumerate(_odf_paragraphs(body), 1):
            builder.add(text, "odt_paragraph", f"paragraph:{number}")
        return ExtractedDocument(builder.text, builder.spans, "text/markdown")
    if suffix == ".odp":
        pages = [el for el in body.iter() if el.tag == f"{_DRAW_NS}page"]
        for number, page in enumerate(pages, 1):
            builder.add(
                "\n\n".join(text for _, text in _odf_paragraphs(page)),
                "odp_slide",
                f"slide:{number}",
            )
        return ExtractedDocument(builder.text, builder.spans, ODP_MEDIA, {"slides": len(pages)})
    sheets = [el for el in body.iter() if el.tag == f"{_TABLE_NS}table"]
    for sheet_index, sheet in enumerate(sheets, 1):
        sheet_name = sheet.get(f"{_TABLE_NS}name") or f"Sheet{sheet_index}"
        row_number = 0
        for row in sheet.iter(f"{_TABLE_NS}table-row"):
            repeat = _safe_int(row.get(f"{_TABLE_NS}number-rows-repeated"))
            cells: list[str] = []
            for cell in row:
                if cell.tag not in {f"{_TABLE_NS}table-cell", f"{_TABLE_NS}covered-table-cell"}:
                    continue
                value = " ".join(text for _, text in _odf_paragraphs(cell))
                if value:
                    cells.extend(
                        [value]
                        * min(
                            MAX_CELL_REPEAT,
                            _safe_int(cell.get(f"{_TABLE_NS}number-columns-repeated")),
                        )
                    )
            row_number += 1
            if cells:
                builder.add(" | ".join(cells), "ods_row", f"sheet:{sheet_name}/row:{row_number}")
            row_number += max(repeat, 1) - 1
    return ExtractedDocument(builder.text, builder.spans, ODS_MEDIA, {"sheets": len(sheets)})


def extract_markitdown(path: Path) -> ExtractedDocument:
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise ExtractionError("markitdown_missing") from exc
    try:
        result = MarkItDown().convert(str(path))
        text = str(getattr(result, "text_content", None) or getattr(result, "markdown", "") or "")
    except Exception as exc:
        raise ExtractionError("markitdown_failed") from exc
    builder = Builder()
    builder.add(text, "markitdown", "document")
    return ExtractedDocument(
        builder.text, builder.spans, "text/markdown", {"converter": "markitdown"}
    )
