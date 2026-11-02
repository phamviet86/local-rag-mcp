"""Vietnamese legal-document helpers: document numbers, headings, structural splitting."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Serial (optional thousands dot), optional year, hyphenated group code starting with letters.
NUM_RE = re.compile(
    r"(?<![\w/])(\d{1,3}(?:\.\d{1,3})*)(?:/(\d{4}))?/([A-Za-zĐđ][\wĐđ]*(?:-[\wĐđ]+)*)(?![\w/])"
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _fold(text: str) -> str:
    text = text.replace("Đ", "D").replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def make_key(text: str) -> str:
    """Fold to a lowercase ASCII key, e.g. ``66.13/2026/NQ-CP`` -> ``66-13-2026-nq-cp``."""
    return _SLUG_RE.sub("-", _fold(text).lower()).strip("-")


def find_numbers(text: str) -> list[str]:
    """Return document numbers (as written) found in text, in order, without duplicates."""
    found: list[str] = []
    for match in NUM_RE.finditer(text):
        serial, year, code = match.groups()
        if not year and re.fullmatch(r"\d{1,2}", serial) and re.fullmatch(r"\d{2,4}", code):
            continue
        value = match.group(0)
        if value not in found:
            found.append(value)
    return found


def is_number_query(text: str) -> bool:
    stripped = text.strip()
    match = NUM_RE.fullmatch(stripped)
    return match is not None


@dataclass(frozen=True)
class Heading:
    level: str  # chuong | muc | dieu
    number: str
    title: str
    start: int
    end: int


_CHAPTER = re.compile(r"^(?:Chương|CHƯƠNG)\s+([IVXLC\d]+)\b[.:]?\s*(.*)$")
_SECTION = re.compile(r"^(?:Mục|MỤC)\s+(\d+[A-Za-z]?)\b[.:]?\s*(.*)$")
_ARTICLE = re.compile(r"^(?:Điều|ĐIỀU)\s+(\d+)([a-zđ]?)\s*[.:]\s*(.*)$")
_CLAUSE = re.compile(r"^(\d{1,2})\.\s+(.*)$")
_POINT = re.compile(r"^([a-zđ])\)\s+(.*)$")
_SENTENCE_END = (".", ";", ":", "!", "?", '"', "”")


def detect_headings(text: str) -> list[Heading]:
    """Detect Chương/Mục/Điều headings, rejecting cross-references inside running text."""
    headings: list[Heading] = []
    last_article = 0
    previous_line = ""
    position = 0
    for raw in text.splitlines(keepends=True):
        line = raw.strip()
        start, end = position, position + len(raw)
        position = end
        if not line:
            continue
        chapter = _CHAPTER.match(line)
        section = _SECTION.match(line)
        article = _ARTICLE.match(line)
        if chapter and len(line) < 200:
            headings.append(Heading("chuong", chapter[1], chapter[2].strip(), start, end))
        elif section and len(line) < 200:
            headings.append(Heading("muc", section[1], section[2].strip(), start, end))
        elif article:
            number, suffix, title = int(article[1]), article[2], article[3].strip()
            sequential = number == last_article + 1 or (suffix and number == last_article)
            ends_sentence = (
                not previous_line
                or previous_line.endswith(_SENTENCE_END)
                or previous_line.isupper()  # chapter / section title line
            )
            # Quoted articles in amending text begin with a quotation mark and are not headings.
            if sequential and len(line) < 200 and ends_sentence:
                headings.append(Heading("dieu", f"{number}{suffix}", title, start, end))
                last_article = number
        previous_line = "" if (chapter or section or article) else line
    return headings


@dataclass
class Section:
    text: str
    start: int
    end: int
    path: list[str] = field(default_factory=list)
    key_parts: list[str] = field(default_factory=list)


def _clause_spans(body: str) -> list[tuple[int, int, str]]:
    """Return (start, end, clause number) for each khoản line in an article body."""
    starts: list[tuple[int, str]] = []
    position = 0
    expected = 1
    for raw in body.splitlines(keepends=True):
        match = _CLAUSE.match(raw.strip())
        if match and int(match[1]) == expected:
            starts.append((position, match[1]))
            expected += 1
        position += len(raw)
    spans = []
    for index, (start, number) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(body)
        spans.append((start, end, number))
    return spans


def _point_spans(body: str) -> list[tuple[int, int, str]]:
    """Return (start, end, letter) for each sequential điểm line (a, b, c, ...)."""
    starts: list[tuple[int, str]] = []
    position = 0
    expected = ord("a")
    for raw in body.splitlines(keepends=True):
        match = _POINT.match(raw.strip())
        if match and ord(match[1]) == expected:
            starts.append((position, match[1]))
            expected += 1
        position += len(raw)
    return [
        (start, starts[i + 1][0] if i + 1 < len(starts) else len(body), letter)
        for i, (start, letter) in enumerate(starts)
    ]


def detect_doc_name(head: str, doc_no: str | None) -> str:
    """Document title: the first short non-heading line after the line holding the number."""
    lines = [line.strip() for line in head.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if doc_no and doc_no in line:
            for candidate in lines[index + 1 : index + 3]:
                if (
                    len(candidate) < 200
                    and not _ARTICLE.match(candidate)
                    and not _CHAPTER.match(candidate)
                ):
                    return candidate
            break
    return ""


def split_sections(
    text: str, doc_no: str | None = None, name: str = "", max_chars: int = 2000
) -> list[Section]:
    """Split legal text into one chunk per Điều (by khoản when too long).

    Returns an empty list when fewer than two article headings are found so callers fall back
    to fixed windows.
    """
    headings = detect_headings(text)
    articles = [h for h in headings if h.level == "dieu"]
    if len(articles) < 2:
        return []
    sections: list[Section] = []
    chapter = ""
    section_name = ""
    context_head = " | ".join(part for part in (doc_no, name) if part)
    for index, heading in enumerate(headings):
        if heading.level == "chuong":
            chapter = f"Chương {heading.number}" + (f". {heading.title}" if heading.title else "")
            section_name = ""
            continue
        if heading.level == "muc":
            section_name = f"Mục {heading.number}" + (f". {heading.title}" if heading.title else "")
            continue
        next_start = len(text)
        for later in headings[index + 1 :]:
            next_start = later.start
            break
        body_start, body_end = heading.start, next_start
        body = text[body_start:body_end]
        label = f"Điều {heading.number}" + (f". {heading.title}" if heading.title else "")
        path = [part for part in (chapter, section_name, label) if part]
        context = " | ".join(part for part in (context_head, chapter, section_name, label) if part)
        base_key = f"dieu-{make_key(heading.number)}"
        pieces: list[tuple[int, int, str | None, str | None]]
        if len(body) <= max_chars:
            pieces = [(0, len(body), None, None)]
        else:
            clauses = _clause_spans(body)
            if not clauses:
                pieces = [(0, len(body), None, None)]
            else:
                pieces = []
                if clauses[0][0] > 0:
                    clauses[0] = (0, clauses[0][1], clauses[0][2])
                for c_start, c_end, number in clauses:
                    points = (
                        _point_spans(body[c_start:c_end]) if c_end - c_start > max_chars else []
                    )
                    if not points:
                        pieces.append((c_start, c_end, number, None))
                        continue
                    pieces.append((c_start, c_start + points[0][0], number, None))
                    for q_start, q_end, letter in points:
                        pieces.append((c_start + q_start, c_start + q_end, number, letter))
        for p_start, p_end, clause, point in pieces:
            raw = body[p_start:p_end]
            left = len(raw) - len(raw.lstrip())
            stripped = raw.strip()
            if not stripped:
                continue
            start = body_start + p_start + left
            piece_path = (
                list(path)
                + ([f"khoản {clause}"] if clause else [])
                + ([f"điểm {point}"] if point else [])
            )
            key = (
                base_key
                + (f"-khoan-{clause}" if clause else "")
                + (f"-diem-{point}" if point else "")
            )
            sections.append(
                Section(
                    f"{context}\n{stripped}" if context else stripped,
                    start,
                    start + len(stripped),
                    piece_path,
                    [key],
                )
            )
    return sections


def split_markdown(text: str, doc_no: str | None = None, name: str = "") -> list[Section]:
    """Split Markdown on ``#`` headings; empty when fewer than two headings exist."""
    marks = [
        (m.start(), len(m[1]), m[2].strip())
        for m in re.finditer(r"^(#{1,6})\s+(.+)$", text, re.MULTILINE)
    ]
    if len(marks) < 2:
        return []
    sections: list[Section] = []
    trail: list[tuple[int, str]] = []
    head = " | ".join(part for part in (doc_no, name) if part)
    for index, (start, level, title) in enumerate(marks):
        trail = [(lv, t) for lv, t in trail if lv < level] + [(level, title)]
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        raw = text[start:end]
        stripped = raw.strip()
        if not stripped:
            continue
        path = [t for _, t in trail]
        context = " | ".join(part for part in (head, *path) if part)
        sections.append(
            Section(
                f"{context}\n{stripped}" if context else stripped,
                start,
                start + len(raw.rstrip()),
                path,
                [f"muc-{make_key(title)[:60] or index}"],
            )
        )
    return sections


def make_cite_key(prefix: str, parts: list[str], taken: set[str]) -> str:
    """Build a unique cite key ``prefix#part`` and register it in ``taken``."""
    base = f"{prefix}#{parts[0]}" if parts else prefix
    key, counter = base, 2
    while key in taken:
        key = f"{base}~{counter}"
        counter += 1
    taken.add(key)
    return key
