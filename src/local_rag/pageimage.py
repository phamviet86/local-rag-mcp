"""Render one PDF page to PNG with the PDFium library installed by the OCR runtime."""

from __future__ import annotations

import ctypes
import struct
import threading
import zlib
from pathlib import Path

MAX_PIXELS = 12_000_000
_LOCK = threading.Lock()
_FPDF_ANNOT = 0x01


def render_page_png(pdf: Path, page: int, library: Path, dpi: int = 110) -> tuple[bytes, int, int]:
    """Return (PNG bytes, width, height) for 1-based ``page`` of ``pdf``."""
    if not 36 <= dpi <= 220:
        raise ValueError("dpi must be between 36 and 220")
    if page < 1:
        raise ValueError("page must be 1 or greater")
    with _LOCK:  # PDFium is not thread-safe.
        lib = ctypes.CDLL(str(library))
        lib.FPDF_InitLibrary()
        lib.FPDF_LoadDocument.restype = ctypes.c_void_p
        lib.FPDF_LoadDocument.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.FPDF_GetPageCount.argtypes = [ctypes.c_void_p]
        lib.FPDF_LoadPage.restype = ctypes.c_void_p
        lib.FPDF_LoadPage.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.FPDF_GetPageWidthF.restype = ctypes.c_float
        lib.FPDF_GetPageWidthF.argtypes = [ctypes.c_void_p]
        lib.FPDF_GetPageHeightF.restype = ctypes.c_float
        lib.FPDF_GetPageHeightF.argtypes = [ctypes.c_void_p]
        lib.FPDFBitmap_Create.restype = ctypes.c_void_p
        lib.FPDFBitmap_Create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.FPDFBitmap_FillRect.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_ulong]
        lib.FPDF_RenderPageBitmap.argtypes = [ctypes.c_void_p] * 2 + [ctypes.c_int] * 6
        lib.FPDFBitmap_GetBuffer.restype = ctypes.c_void_p
        lib.FPDFBitmap_GetBuffer.argtypes = [ctypes.c_void_p]
        lib.FPDFBitmap_GetStride.argtypes = [ctypes.c_void_p]
        for closer in (lib.FPDFBitmap_Destroy, lib.FPDF_ClosePage, lib.FPDF_CloseDocument):
            closer.argtypes = [ctypes.c_void_p]
        document = lib.FPDF_LoadDocument(str(pdf).encode(), None)
        if not document:
            raise ValueError("PDFium could not open the document")
        try:
            count = lib.FPDF_GetPageCount(document)
            if page > count:
                raise ValueError(f"page {page} is outside 1..{count}")
            handle = lib.FPDF_LoadPage(document, page - 1)
            if not handle:
                raise ValueError("PDFium could not load the page")
            try:
                scale = dpi / 72
                width = max(1, round(lib.FPDF_GetPageWidthF(handle) * scale))
                height = max(1, round(lib.FPDF_GetPageHeightF(handle) * scale))
                if width * height > MAX_PIXELS:
                    raise ValueError("page image would be too large; lower dpi")
                bitmap = lib.FPDFBitmap_Create(width, height, 0)
                try:
                    lib.FPDFBitmap_FillRect(bitmap, 0, 0, width, height, 0xFFFFFFFF)
                    lib.FPDF_RenderPageBitmap(bitmap, handle, 0, 0, width, height, 0, _FPDF_ANNOT)
                    stride = lib.FPDFBitmap_GetStride(bitmap)
                    buffer = ctypes.string_at(lib.FPDFBitmap_GetBuffer(bitmap), stride * height)
                finally:
                    lib.FPDFBitmap_Destroy(bitmap)
            finally:
                lib.FPDF_ClosePage(handle)
        finally:
            lib.FPDF_CloseDocument(document)
    return _png(buffer, width, height, stride), width, height


def _png(bgra: bytes, width: int, height: int, stride: int) -> bytes:
    rows = bytearray()
    for y in range(height):
        row = bgra[y * stride : y * stride + width * 4]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        rows += b"\x00" + rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
