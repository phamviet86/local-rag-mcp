"""Durable Drive sync coverage, separate from text results and job progress."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .db import Database

KEY = "drive_coverage_v1"
RETRY = (
    "Ask an admin to retry reconcile; check file access/export or repair the file if it persists."
)


def load_coverage(db: Database, source_id: str) -> dict[str, Any]:
    with db.connect() as connection:
        row = connection.execute(
            "SELECT value_json FROM source_sync_state WHERE source_id=? AND key=?",
            (source_id, KEY),
        ).fetchone()
    return (
        json.loads(row[0])
        if row
        else {
            "listing_complete": False,
            "files": {},
            "listing_failures": [],
            "running": False,
        }
    )


def save_coverage(db: Database, source_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO source_sync_state(source_id,key,value_json) VALUES(?,?,?)
               ON CONFLICT(source_id,key) DO UPDATE SET value_json=excluded.value_json,
               updated_at=CURRENT_TIMESTAMP""",
            (source_id, KEY, json.dumps(state)),
        )


def source_failure(db: Database, source_id: str, reason: str) -> None:
    state = load_coverage(db, source_id)
    state.update(
        running=False,
        listing_complete=False,
        source_error={
            "stage": "source",
            "reason": reason,
            "action": "Ask an admin to check Drive access, configuration and connectivity; retry.",
        },
    )
    save_coverage(db, source_id, state)


def matches(record: dict[str, Any], folder: str | None) -> bool:
    if not folder:
        return True
    if folder.startswith("id:"):
        identifier = folder[3:]
        return identifier == record.get("file_id") or identifier in [
            *record.get("ancestor_ids", []),
            *record.get("indexed_ancestor_ids", []),
        ]
    return any(
        path == folder or path.startswith(folder.rstrip("/") + "/")
        for path in (record.get("path", ""), record.get("indexed_path", ""))
        if path
    )


def index_coverage(
    db: Database,
    source: str | None = None,
    folder: str | None = None,
    offset: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("coverage offset must be nonnegative and limit must be 1..100")
    summaries: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for record in db.sources(enabled=True):
        if record["kind"] != "google_drive" or (
            source and source not in {record["id"], record["name"]}
        ):
            continue
        state = load_coverage(db, record["id"])
        failures = [item for item in state["files"].values() if matches(item, folder)]
        # Unknown subtrees make totals uncertain; only disclose matching failure names.
        listing = [item for item in state["listing_failures"] if matches(item, folder)]
        pending = sum(item["status"] == "pending" for item in failures)
        failed = len(failures) - pending + sum(item["stage"] == "metadata" for item in listing)
        listing_complete = state["listing_complete"]
        status = (
            "partial"
            if failed or listing
            else "pending"
            if pending or state["running"]
            else ("complete" if listing_complete else "unknown")
        )
        summaries.append(
            {
                "source": record["name"],
                "source_id": record["id"],
                "status": status,
                "listing_complete": listing_complete,
                "total_files_known": listing_complete,
                "failed_files": failed,
                "pending_files": pending,
                "updated_at": state.get("updated_at"),
                "source_error": state.get("source_error"),
            }
        )
        for item in [*failures, *listing]:
            details.append({"source": record["name"], "source_id": record["id"], **item})
    details.sort(key=lambda item: (item["source_id"], item.get("file_id", ""), item["stage"]))
    incomplete = any(item["status"] != "complete" for item in summaries)
    return {
        "kind": "drive_sync",
        "status": "partial" if incomplete else "complete",
        "scope": folder,
        "sources": summaries,
        "issues": details[offset : offset + limit],
        "total_issues": len(details),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(details) else None,
        "notice": (
            "Results cover indexed content only. Report failed/stale files and uncertain coverage; "
            "zero matches does not establish absence in unindexed files. "
            "Use index_coverage with the same source/folder and next_offset for more issues."
        )
        if incomplete
        else None,
    }
