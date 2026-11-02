"""Legal validity dates and read-only status checks. "Unknown" is never treated as valid."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .db import Database, stable_ref
from .legal import find_numbers, make_key

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
FIELDS = (
    "effective_from",
    "effective_to",
    "amended_by",
    "replaced_by",
    "status_checked_on",
    "status_source",
)


def normalize_validity(fields: dict[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unsupported validity fields: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {name: fields.get(name) or None for name in FIELDS}
    for name in ("effective_from", "effective_to", "status_checked_on"):
        value = values[name]
        if value is not None and not (isinstance(value, str) and _DATE.fullmatch(value)):
            raise ValueError(f"{name} must be YYYY-MM-DD")
    start, end = values["effective_from"], values["effective_to"]
    if end is not None and (start is None or end <= start):
        raise ValueError("effective_to is exclusive and must be after effective_from")
    amended = fields.get("amended_by") or []
    if not isinstance(amended, list) or not all(isinstance(v, str) for v in amended):
        raise ValueError("amended_by must be a list of document numbers")
    values["amended_by"] = amended
    for name in ("replaced_by", "status_source"):
        if values[name] is not None and not isinstance(values[name], str):
            raise ValueError(f"{name} must be a string")
    return values


def status_on(row: Any, day: str) -> str:
    """valid | not_yet_effective | expired | unknown; the interval is [from, to)."""
    if row is None or row["effective_from"] is None:
        return "unknown"
    if day < row["effective_from"]:
        return "not_yet_effective"
    if row["effective_to"] is not None and day >= row["effective_to"]:
        return "expired"
    return "valid"


def check_status(db: Database, path: str, on: str, source: str | None = None) -> dict[str, Any]:
    """Validity, cited-expired warnings, duplicates and integrity for one document."""
    if not _DATE.fullmatch(on):
        raise ValueError("on must be YYYY-MM-DD")
    document = db.resolve_document(path, source)
    warnings: list[str] = []
    with db.connect() as connection:
        validity = connection.execute(
            "SELECT * FROM document_validity WHERE document_id=?", (document["id"],)
        ).fetchone()
        status = status_on(validity, on)
        if status == "unknown":
            warnings.append("UNKNOWN_VALIDITY: no effective date recorded; do not treat as valid")
        elif status != "valid":
            warnings.append(f"NOT_VALID_ON_DATE: {status}")
        if validity is not None and validity["replaced_by"]:
            warnings.append(f"REPLACED_BY: {validity['replaced_by']}")
        # Documents cited in the text whose recorded validity has ended on the date.
        text = " ".join(
            row[0]
            for row in connection.execute(
                "SELECT text FROM chunks WHERE document_id=?", (document["id"],)
            )
        )
        cited_expired = []
        for number in find_numbers(text):
            target = connection.execute(
                """SELECT d.id,d.doc_no,v.effective_from,v.effective_to FROM documents d
                   JOIN document_validity v ON v.document_id=d.id
                   WHERE d.doc_no_key=? AND d.id<>?""",
                (make_key(number), document["id"]),
            ).fetchone()
            if target is not None and status_on(target, on) == "expired":
                cited_expired.append(target["doc_no"])
        if cited_expired:
            warnings.append(f"CITES_EXPIRED: {', '.join(sorted(set(cited_expired)))}")
        same_hash = [
            stable_ref({**dict(row), "source_kind": row["source_kind"]})
            for row in connection.execute(
                """SELECT d.id,d.source_id,d.external_id,s.kind source_kind FROM documents d
                   JOIN sources s ON s.id=d.source_id
                   WHERE d.content_hash=? AND d.id<>?""",
                (document["content_hash"], document["id"]),
            )
        ]
        if same_hash:
            warnings.append("DUPLICATE_SHA256: identical file content indexed more than once")
        same_content = [
            row[0]
            for row in connection.execute(
                """SELECT DISTINCT d.doc_no FROM chunks c JOIN documents d ON d.id=c.document_id
                   WHERE d.id<>? AND d.content_hash<>? AND c.chunk_hash IN
                     (SELECT chunk_hash FROM chunks WHERE document_id=?)""",
                (document["id"], document["content_hash"], document["id"]),
            )
        ]
        if same_content:
            warnings.append("DUPLICATE_CONTENT: other files share indexed chunks")
        ocr_pages = connection.execute(
            "SELECT count(*) FROM chunks WHERE document_id=? AND ocr=1", (document["id"],)
        ).fetchone()[0]
        open_reviews = connection.execute(
            "SELECT count(*) FROM reviews WHERE document_id=? AND status='open'",
            (document["id"],),
        ).fetchone()[0]
    if ocr_pages:
        warnings.append("OCR_UNVERIFIED: some chunks come from OCR; open the original page")
    if open_reviews:
        warnings.append("OPEN_REVIEWS: pages await review")
    integrity = _integrity(document)
    if integrity["sha256_matches"] is False:
        warnings.append("SOURCE_CHANGED: file hash differs from the indexed hash; reindex")
    return {
        "stable_ref": stable_ref(document),
        "doc_no": document["doc_no"],
        "on": on,
        "status": status,
        "validity": None
        if validity is None
        else {
            **{k: validity[k] for k in FIELDS if k != "amended_by"},
            "amended_by": json.loads(validity["amended_by_json"]),
        },
        "duplicates": {"same_sha256": same_hash, "same_content_count": len(same_content)},
        "integrity": integrity,
        "warnings": warnings,
    }


def _integrity(document: Any) -> dict[str, Any]:
    """Re-hash a local source file; Drive and missing files report ``None``/not-checked."""
    if document["source_kind"] != "local":
        return {"sha256_matches": None, "checked": False}
    path = Path(document["path"])
    if not path.is_file():
        return {"sha256_matches": None, "checked": False, "reason": "file_missing"}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256_matches": digest.hexdigest() == document["content_hash"], "checked": True}
