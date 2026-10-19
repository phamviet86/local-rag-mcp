"""Narrow, byte-preserving repair of hash-prefixed SpreadsheetML colors."""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers import expat
from zipfile import ZipFile

from .extraction_errors import MAX_FILE_BYTES, ExtractionError

MAX_MEMBERS = 10_000
MAX_STYLES_BYTES = 16 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
# openpyxl 3.1.5 apply_stylesheet uses ARC_STYLE, not workbook relationships.
STYLES_PART = "xl/styles.xml"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
COLOR = re.compile(r"(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\Z")
TAG = re.compile(rb"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
RGB = re.compile(rb"(?<=\s)rgb\s*=\s*([\"'])(#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?)\1")


def exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    while error not in chain:
        chain.append(error)
        following = error.__cause__ or error.__context__
        if following is None:
            break
        error = following
    return chain


def color_failure(error: BaseException) -> bool:
    return any(
        isinstance(item, ValueError) and str(item) == "Colors must be aRGB hex values"
        for item in exception_chain(error)
    )


def stylesheet_failure(error: BaseException) -> bool:
    for item in exception_chain(error):
        if "could not read stylesheet" in str(item):
            return True
        trace = item.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_name == "apply_stylesheet":
                return True
            trace = trace.tb_next
    return False


def check_xml(data: bytes) -> None:
    # Also recognize UTF-16/32 declarations before handing bytes to any XML parser.
    probe = data.replace(b"\x00", b"")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", probe, re.IGNORECASE):
        raise ExtractionError("xlsx_stylesheet_unreadable")


def inspect_archive(path: Path) -> bytes | None:
    """Bound decompression before openpyxl reads even the original workbook."""
    with ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_FILE_BYTES:
            raise ExtractionError("xlsx_archive_limit")
        if len({m.filename for m in members}) != len(members):
            raise ExtractionError("xlsx_parse_failed")
        for member in members:
            if member.file_size > MAX_COMPRESSION_RATIO * max(member.compress_size, 1):
                raise ExtractionError("xlsx_archive_limit")
        # openpyxl silently skips a missing worksheet part. Refuse partial output.
        if "xl/_rels/workbook.xml.rels" in archive.namelist():
            parser = expat.ParserCreate(namespace_separator="}")

            def relationship(name: str, attrs: dict[str, str]) -> None:
                if not name.endswith("}Relationship") or not attrs.get("Type", "").endswith(
                    "/worksheet"
                ):
                    return
                target = attrs.get("Target", "")
                part = posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
                if attrs.get("TargetMode") == "External" or part not in archive.namelist():
                    raise ExtractionError("xlsx_parse_failed")

            def reject_declaration(*args: object) -> None:
                raise ExtractionError("xlsx_parse_failed")

            parser.StartElementHandler = relationship
            parser.StartDoctypeDeclHandler = reject_declaration
            parser.EntityDeclHandler = reject_declaration
            with archive.open("xl/_rels/workbook.xml.rels") as stream:
                while chunk := stream.read(64 * 1024):
                    parser.Parse(chunk, False)
                parser.Parse(b"", True)
        if STYLES_PART not in archive.namelist():
            return None
        if archive.getinfo(STYLES_PART).file_size > MAX_STYLES_BYTES:
            raise ExtractionError("xlsx_archive_limit")
        data = archive.read(STYLES_PART)
        check_xml(data)
        return data


def repair_styles(data: bytes) -> tuple[bytes, int]:
    check_xml(data)
    original = ET.fromstring(data)
    count = 0
    for element in original.iter():
        if not element.tag.startswith("{" + MAIN_NS + "}") or "rgb" not in element.attrib:
            continue
        value = element.attrib["rgb"]
        candidate = value[1:] if value.startswith("#") else value
        if not COLOR.fullmatch(candidate):
            raise ExtractionError("xlsx_unsupported_color")
        if value.startswith("#"):
            element.attrib["rgb"] = candidate
            count += 1
    if not count:
        return data, 0
    # Expat resolves namespace scope and supplies byte offsets. Only a literal hash
    # in the unqualified rgb attribute of a main-namespace start tag is removed.
    parser = expat.ParserCreate(namespace_separator="}")
    removals: list[int] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        if not name.startswith(MAIN_NS + "}") or not attrs.get("rgb", "").startswith("#"):
            return
        tag = TAG.match(data, parser.CurrentByteIndex)
        if tag is None:
            raise ExtractionError("xlsx_stylesheet_unreadable")
        matches = list(RGB.finditer(tag.group()))
        if len(matches) != 1:
            raise ExtractionError("xlsx_stylesheet_unreadable")
        removals.append(tag.start() + matches[0].start(2))

    parser.StartElementHandler = start
    parser.Parse(data, True)
    if len(removals) != count:
        raise ExtractionError("xlsx_stylesheet_unreadable")
    pieces, offset = [], 0
    for index in removals:
        pieces.append(data[offset:index])
        offset = index + 1
    pieces.append(data[offset:])
    repaired = b"".join(pieces)
    # Equality includes all text, tails, attributes, namespaces and element order;
    # the original tree above has ONLY the approved rgb attribute changes.
    result = ET.fromstring(repaired)
    if ET.tostring(result) != ET.tostring(original):
        raise ExtractionError("xlsx_stylesheet_unreadable")
    return repaired, count


def copy_archive(source: Path, destination: Path, styles: bytes) -> None:
    total = 0
    with ZipFile(source) as archive, ZipFile(destination, "w") as output:
        output.comment = archive.comment
        for member in archive.infolist():
            if member.filename == STYLES_PART:
                output.writestr(member, styles)
                total += len(styles)
                continue
            # Stream payloads, preserving member order, names, metadata and compression.
            with archive.open(member) as reader, output.open(member, "w") as writer:
                while block := reader.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_FILE_BYTES:
                        raise ExtractionError("xlsx_archive_limit")
                    writer.write(block)
