"""Vietnamese legal-document helpers: document numbers, headings, structural splitting."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Serial (optional thousands dot), optional year, hyphenated group code. The code must be
# upper-case so that "984/Ngày 25-8-2024" is not read as a document number.
NUM_RE = re.compile(
    r"(?<![\w/])(\d{1,3}(?:\.\d{1,3})*)(?:/(\d{4}))?/([A-ZĐ][A-ZĐ0-9]*(?:-[A-ZĐ0-9]+)*)(?![\w/])"
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_OWN_NUMBER_LINE = re.compile(r"^\W*(?:(?:Luật|Pháp lệnh|Nghị quyết|Bộ luật)\s+)?[Ss]ố\s*:?\s*$")
_REFERENCE_BEFORE = re.compile(r"(?:định|tư|quyết|lệnh|luật)\s+(?:số\s*:?\s*)?$", re.IGNORECASE)


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
    return NUM_RE.fullmatch(text.strip()) is not None


_TYPE_CODES = (
    ("THÔNG TƯ", ("TT",)),
    ("NGHỊ ĐỊNH", ("NĐ",)),
    ("NGHỊ QUYẾT", ("NQ", "QH")),
    ("QUYẾT ĐỊNH", ("QĐ",)),
    ("PHÁP LỆNH", ("PL", "UBTVQH")),
    ("LUẬT", ("QH",)),
)


def _document_type_codes(plain: str) -> tuple[str, ...]:
    """Number-code fragments expected for the document type named near the top, if any."""
    head = plain[:1500].replace("Ƣ", "Ư").replace("ƣ", "ư").upper()
    found = [(head.find(kind), codes) for kind, codes in _TYPE_CODES if kind in head]
    return min(found)[1] if found else ()


def find_own_number(text: str) -> str | None:
    """The document's own number, preferring ``Số: N`` / ``Luật số: N`` over cited references.

    A consolidated or draft text without its own number falls back to the first cited number
    whose code fits the document type (a Thông tư citing a Nghị định does not take its number).
    """
    plain, _ = normalize_markup(text)
    plain = re.sub(r"(?<=\d)[ \t]+/", "/", plain)
    plain = re.sub(r"/[ \t]+(?=[A-ZĐ\d])", "/", plain)
    for match in NUM_RE.finditer(plain):
        line_start = plain.rfind("\n", 0, match.start()) + 1
        if _OWN_NUMBER_LINE.match(plain[line_start : match.start()]):
            return match.group(0)
    for match in NUM_RE.finditer(plain):
        before = plain[max(0, match.start() - 60) : match.start()]
        if re.search(r"[Ss]ố\s*:\s*$", before):  # "Luật số: 61/..." is the document's own
            return match.group(0)
        if re.search(r"[Ss]ố\s*$", before) and not _REFERENCE_BEFORE.search(before.rstrip()):
            return match.group(0)
    codes = _document_type_codes(plain)
    for number in find_numbers(plain):
        if not codes or any(code in number.upper() for code in codes):
            return number
    return None


# --- Markdown noise removal -------------------------------------------------------------------

_MARKUP = re.compile(r"(?m)^#{1,6}[ \t]+|</?u>|\*\*|__|\*")
_BOLD_HEADING = re.compile(r"\s*(?:Điều\s+\d|Chương\s+[IVXLC\d]|Mục\s+\d|ĐIỀU|CHƯƠNG|MỤC)")
_INLINE_ARTICLE = re.compile(r"(?<=[.;:!?])[ \t]+(?=Điều\s+\d+[a-zđ]?\s*\.)")


def normalize_markup(text: str) -> tuple[str, list[int]]:
    """Strip Markdown emphasis/heading markers and break inline ``Điều N.`` onto its own line.

    Returns the plain text and, for every plain character, its index in the original text.
    """
    parts: list[str] = []
    index: list[int] = []
    position = 0
    in_heading = False
    for match in _MARKUP.finditer(text):
        parts.append(text[position : match.start()])
        index.extend(range(position, match.start()))
        position = match.end()
        if match[0] != "**":
            continue
        # A bold "Điều N. ..." / "Chương ..." / "Mục ..." span is a heading: give it its own lines.
        if in_heading or _BOLD_HEADING.match(text, position):
            parts.append("\n\n")
            index.extend((match.start(), match.start()))
            in_heading = not in_heading
    parts.append(text[position:])
    index.extend(range(position, len(text)))
    plain = "".join(parts)

    out: list[str] = []
    out_index: list[int] = []
    position = 0
    for match in _INLINE_ARTICLE.finditer(plain):
        out.append(plain[position : match.start()])
        out_index.extend(index[position : match.start()])
        out.append("\n\n")
        out_index.extend((index[match.start()], index[match.start()]))
        position = match.end()
    out.append(plain[position:])
    out_index.extend(index[position:])
    return "".join(out), out_index


# --- Headings ---------------------------------------------------------------------------------


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
_APPENDIX = re.compile(
    r"^[|\s]*(?:\(Kèm theo\b|(?:Phụ lục|PHỤ LỤC)\b(?!.*\b(?:này|ban hành|kèm theo)\b))"
)
_CLAUSE = re.compile(r"^(\d{1,2})\.\s+(.*)$")
_POINT = re.compile(r"^([a-zđ])\)\s+(.*)$")
_SENTENCE_END = (".", ";", ":", "!", "?", '"', "”")


def detect_headings(text: str) -> list[Heading]:
    """Detect Chương/Mục/Điều headings, rejecting cross-references inside running text."""
    headings: list[Heading] = []
    last_article = 0
    previous_line = ""
    appendix_open = False
    lines = text.splitlines(keepends=True)
    position = 0
    for number, raw in enumerate(lines):
        line = raw.strip()
        start, end = position, position + len(raw)
        position = end
        if not line:
            continue
        chapter = _CHAPTER.match(line)
        section = _SECTION.match(line)
        article = _ARTICLE.match(line)
        if headings and len(line) <= 200 and _APPENDIX.match(line) and not appendix_open:
            # Forms after the last article ("Phụ lục", "(Kèm theo ...)") are not part of it.
            appendix_open = True
            headings.append(Heading("phuluc", "", line.strip("| ")[:120], start, end))
        elif appendix_open:
            pass  # numbering inside forms never starts a new article
        elif chapter and len(line) < 200:
            headings.append(Heading("chuong", chapter[1], chapter[2].strip(), start, end))
        elif section and len(line) < 200:
            headings.append(Heading("muc", section[1], section[2].strip(), start, end))
        elif article:
            value, suffix, title = int(article[1]), article[2], article[3].strip()
            # Consolidated texts skip repealed articles, so allow a small forward jump; numbering
            # that restarts (forms in appendices) is never a new article.
            sequential = last_article < value <= last_article + 10 or bool(
                suffix and value == last_article
            )
            ends_sentence = (
                value == last_article + 1  # page running headers can precede a true heading
                or not previous_line
                or previous_line.endswith(_SENTENCE_END)
                or previous_line.isupper()  # chapter / section title line
            )
            # Quoted articles in amending text begin with a quotation mark and are not headings.
            if sequential and len(line) < 200 and ends_sentence:
                if not title:
                    title = _next_title(lines, number)
                headings.append(Heading("dieu", f"{value}{suffix}", title, start, end))
                last_article = value
        previous_line = "" if (chapter or section or article) else line
    return headings


def _next_title(lines: list[str], number: int) -> str:
    """Title on the line after a bare ``Điều N.`` heading."""
    for raw in lines[number + 1 : number + 4]:
        line = raw.strip()
        if not line:
            continue
        if len(line) < 200 and not (
            _ARTICLE.match(line) or _CHAPTER.match(line) or _CLAUSE.match(line)
        ):
            return line
        break
    return ""


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
    return [
        (start, starts[i + 1][0] if i + 1 < len(starts) else len(body), number)
        for i, (start, number) in enumerate(starts)
    ]


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


def _pack_paragraphs(body: str, limit: int) -> list[tuple[int, int]]:
    """Greedy-pack blank-line separated paragraphs into spans of about ``limit`` characters."""
    spans: list[tuple[int, int]] = []
    start = 0
    cursor = 0
    for match in re.finditer(r"\n\s*\n", body):
        if match.start() - start > limit and cursor > start:
            spans.append((start, cursor))
            start = cursor
        cursor = match.end()
    spans.append((start, len(body)))
    return spans


_KINDS = ("LUẬT", "NGHỊ ĐỊNH", "THÔNG TƯ", "NGHỊ QUYẾT", "QUYẾT ĐỊNH", "PHÁP LỆNH", "BỘ LUẬT")


def detect_doc_name(head: str, doc_no: str | None) -> str:
    """Document title: the short line after the type heading (THÔNG TƯ, NGHỊ ĐỊNH, ...)."""
    plain, _ = normalize_markup(head)
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        candidates = lines[index + 1 : index + 3]
        if line.upper() in _KINDS:
            if not candidates or len(candidates[0]) >= 200:
                continue
            return f"Luật {candidates[0]}" if line.upper() == "LUẬT" else candidates[0]
        if doc_no and doc_no in line:
            for candidate in candidates:
                if (
                    len(candidate) < 200
                    and not _ARTICLE.match(candidate)
                    and not _CHAPTER.match(candidate)
                    and candidate.upper() not in _KINDS
                ):
                    return candidate
    return ""


def split_sections(
    text: str, doc_no: str | None = None, name: str = "", max_chars: int = 2000
) -> list[Section]:
    """Split legal text into one chunk per Điều (by khoản, điểm or paragraphs when too long).

    Markdown emphasis from PDF extraction is removed from chunk text; offsets refer to the
    original ``text``. Text before the first heading becomes a ``mo-dau`` chunk. Returns an empty
    list when fewer than two article headings are found so callers fall back to fixed windows.
    """
    plain, origin = normalize_markup(text)
    headings = detect_headings(plain)
    if sum(h.level == "dieu" for h in headings) < 2:
        return []

    sections: list[Section] = []
    context_head = " | ".join(part for part in (doc_no, name) if part)

    def emit(
        body: str, body_start: int, a: int, b: int, context: str, path: list[str], key: str
    ) -> None:
        raw = body[a:b]
        stripped = raw.strip()
        if not stripped:
            return
        begin = body_start + a + len(raw) - len(raw.lstrip())
        start, end = origin[begin], origin[begin + len(stripped) - 1] + 1
        sections.append(
            Section(f"{context}\n{stripped}" if context else stripped, start, end, path, [key])
        )

    def emit_packed(
        body: str, body_start: int, a: int, b: int, context: str, path: list[str], key: str
    ) -> None:
        for number, (x, y) in enumerate(_pack_paragraphs(body[a:b], max_chars), 1):
            part_key = key if number == 1 else f"{key}-pt{number}"
            emit(body, body_start, a + x, a + y, context, path, part_key)

    if plain[: headings[0].start].strip():
        emit_packed(plain, 0, 0, headings[0].start, context_head, ["Mở đầu"], "mo-dau")

    chapter = ""
    section_name = ""
    for index, heading in enumerate(headings):
        if heading.level == "phuluc":
            body = plain[heading.start :]
            emit_packed(
                body,
                heading.start,
                0,
                len(body),
                " | ".join(part for part in (context_head, "Phụ lục") if part),
                ["Phụ lục"],
                "phu-luc",
            )
            break
        if heading.level == "chuong":
            chapter = f"Chương {heading.number}" + (f". {heading.title}" if heading.title else "")
            section_name = ""
            continue
        if heading.level == "muc":
            section_name = f"Mục {heading.number}" + (f". {heading.title}" if heading.title else "")
            continue
        end_of_body = headings[index + 1].start if index + 1 < len(headings) else len(plain)
        body_start = heading.start
        body = plain[body_start:end_of_body]
        label = f"Điều {heading.number}" + (f". {heading.title}" if heading.title else "")
        path = [part for part in (chapter, section_name, label) if part]
        context = " | ".join(part for part in (context_head, chapter, section_name, label) if part)
        base_key = f"dieu-{make_key(heading.number)}"
        if len(body) <= max_chars:
            emit(body, body_start, 0, len(body), context, path, base_key)
            continue
        clauses = _clause_spans(body)
        if not clauses:
            emit_packed(body, body_start, 0, len(body), context, path, base_key)
            continue
        clauses[0] = (0, clauses[0][1], clauses[0][2])
        for c_start, c_end, number in clauses:
            clause_path = [*path, f"khoản {number}"]
            clause_key = f"{base_key}-khoan-{number}"
            points = _point_spans(body[c_start:c_end]) if c_end - c_start > max_chars else []
            if not points:
                emit_packed(body, body_start, c_start, c_end, context, clause_path, clause_key)
                continue
            emit(
                body, body_start, c_start, c_start + points[0][0], context, clause_path, clause_key
            )
            for q_start, q_end, letter in points:
                emit_packed(
                    body,
                    body_start,
                    c_start + q_start,
                    c_start + q_end,
                    context,
                    [*clause_path, f"điểm {letter}"],
                    f"{clause_key}-diem-{letter}",
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
