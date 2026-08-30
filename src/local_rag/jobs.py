from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

from .db import Database

ACTIVE_STATES = ("queued", "running")


class IndexJobManager:
    """Durable single-writer coordination; committed WAL readers remain independent."""

    def __init__(self, database: Database):
        self.db = database
        self._threads: dict[str, threading.Thread] = {}

    def enqueue(
        self,
        kind: str,
        source: str | None = None,
        target: str | None = None,
        *,
        reextract: bool = False,
        full: bool = False,
    ) -> dict[str, Any]:
        if kind not in {"reconcile", "reindex"}:
            raise ValueError("job kind must be reconcile or reindex")
        identifier = uuid.uuid4().hex
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE index_jobs SET state='error',phase='error',
                   error='writer heartbeat expired',completed_at=CURRENT_TIMESTAMP
                   WHERE state='running' AND heartbeat_at < datetime('now','-20 minutes')"""
            )
            connection.execute(
                """UPDATE index_writer_lease SET job_id=NULL
                   WHERE job_id IN (SELECT id FROM index_jobs WHERE state='error')"""
            )
            active = connection.execute(
                """SELECT * FROM index_jobs WHERE state IN ('queued','running')
                   ORDER BY created_at,id LIMIT 1"""
            ).fetchone()
            if active is not None:
                same = (
                    active["kind"] == kind
                    and active["source"] == source
                    and active["target"] == target
                    and bool(active["reextract"]) == reextract
                    and bool(active["full"]) == full
                )
                if same:
                    result = _job(active, reader=False)
                    result["coalesced"] = True
                    return result
                connection.execute(
                    """INSERT INTO index_jobs
                       (id,kind,source,target,reextract,full,state,phase,error,completed_at)
                       VALUES(?,?,?,?,?,?,'rejected','rejected',?,CURRENT_TIMESTAMP)""",
                    (
                        identifier,
                        kind,
                        source,
                        target,
                        int(reextract),
                        int(full),
                        "another indexing job is active",
                    ),
                )
                rejected = connection.execute(
                    "SELECT * FROM index_jobs WHERE id=?", (identifier,)
                ).fetchone()
                return _job(rejected, reader=False)
            connection.execute(
                """INSERT INTO index_jobs(id,kind,source,target,reextract,full,state,phase)
                   VALUES(?,?,?,?,?,?,'queued','queued')""",
                (identifier, kind, source, target, int(reextract), int(full)),
            )
            row = connection.execute(
                "SELECT * FROM index_jobs WHERE id=?", (identifier,)
            ).fetchone()
        return _job(row, reader=False)

    def run(
        self,
        job_id: str,
        execute: Callable[[dict[str, Any], Callable[[str], None]], dict[str, Any]],
        embedding_pending: Callable[[], int],
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM index_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError(f"job not found: {job_id}")
            if row["state"] not in ACTIVE_STATES:
                return _job(row, reader=False)
            lease = connection.execute(
                "SELECT job_id FROM index_writer_lease WHERE singleton=1"
            ).fetchone()
            if lease["job_id"] not in {None, job_id}:
                raise RuntimeError("index writer lease is held by another job")
            connection.execute(
                """UPDATE index_writer_lease SET job_id=?,heartbeat_at=CURRENT_TIMESTAMP
                   WHERE singleton=1""",
                (job_id,),
            )
            connection.execute(
                """UPDATE index_jobs SET state='running',phase='discovering',
                   started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
                   heartbeat_at=CURRENT_TIMESTAMP WHERE id=?""",
                (job_id,),
            )
            row = connection.execute("SELECT * FROM index_jobs WHERE id=?", (job_id,)).fetchone()

        def phase(value: str) -> None:
            with self.db.connect() as connection:
                connection.execute(
                    """UPDATE index_jobs SET phase=?,heartbeat_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (value, job_id),
                )
                connection.execute(
                    """UPDATE index_writer_lease SET heartbeat_at=CURRENT_TIMESTAMP
                       WHERE singleton=1 AND job_id=?""",
                    (job_id,),
                )

        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(5):
                try:
                    with self.db.connect() as connection:
                        connection.execute(
                            """UPDATE index_jobs SET heartbeat_at=CURRENT_TIMESTAMP WHERE id=?""",
                            (job_id,),
                        )
                        connection.execute(
                            """UPDATE index_writer_lease SET heartbeat_at=CURRENT_TIMESTAMP
                               WHERE singleton=1 AND job_id=?""",
                            (job_id,),
                        )
                except Exception:
                    return

        heartbeat_thread = threading.Thread(
            target=heartbeat, name=f"local-rag-heartbeat-{job_id[:8]}", daemon=True
        )
        heartbeat_thread.start()
        try:
            phase("indexing")
            report = execute(_job(row, reader=False), phase)
            with self.db.connect() as connection:
                live = connection.execute(
                    "SELECT discovered,processed,searchable FROM index_jobs WHERE id=?", (job_id,)
                ).fetchone()
            counts = (
                (int(live["discovered"]), int(live["processed"]), int(live["searchable"]))
                if live is not None and int(live["discovered"])
                else _report_counts(report)
            )
            phase("finalizing")
            pending = embedding_pending()
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE index_jobs SET state='complete',phase='complete',discovered=?,
                       processed=?,searchable=?,remaining=0,embedding_pending=?,
                       heartbeat_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (*counts, pending, job_id),
                )
                connection.execute(
                    """UPDATE index_writer_lease SET job_id=NULL,heartbeat_at=CURRENT_TIMESTAMP
                       WHERE singleton=1 AND job_id=?""",
                    (job_id,),
                )
        except Exception as exc:
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE index_jobs SET state='error',phase='error',error=?,
                       heartbeat_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (str(exc), job_id),
                )
                connection.execute(
                    """UPDATE index_writer_lease SET job_id=NULL,heartbeat_at=CURRENT_TIMESTAMP
                       WHERE singleton=1 AND job_id=?""",
                    (job_id,),
                )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
        return self.status(job_id, reader=False)

    def start_background(
        self,
        job_id: str,
        execute: Callable[[dict[str, Any], Callable[[str], None]], dict[str, Any]],
        embedding_pending: Callable[[], int],
    ) -> dict[str, Any]:
        thread = threading.Thread(
            target=self.run,
            args=(job_id, execute, embedding_pending),
            name=f"local-rag-index-{job_id[:8]}",
            daemon=True,
        )
        self._threads[job_id] = thread
        thread.start()
        return self.status(job_id, reader=False)

    def status(self, job_id: str, *, reader: bool = True) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM index_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"job not found: {job_id}")
        return _job(row, reader=reader)

    def progress(
        self,
        job_id: str,
        *,
        discovered: int,
        processed: int,
        searchable: int,
        remaining: int,
        embedding_pending: int | None = None,
    ) -> None:
        with self.db.connect() as connection:
            if embedding_pending is None:
                connection.execute(
                    """UPDATE index_jobs SET discovered=?,processed=?,searchable=?,remaining=?,
                       heartbeat_at=CURRENT_TIMESTAMP WHERE id=? AND state='running'""",
                    (discovered, processed, searchable, remaining, job_id),
                )
            else:
                connection.execute(
                    """UPDATE index_jobs SET discovered=?,processed=?,searchable=?,remaining=?,
                       embedding_pending=?,heartbeat_at=CURRENT_TIMESTAMP
                       WHERE id=? AND state='running'""",
                    (
                        discovered,
                        processed,
                        searchable,
                        remaining,
                        embedding_pending,
                        job_id,
                    ),
                )

    def list(self, *, reader: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM index_jobs ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_job(row, reader=reader) for row in rows]

    def index_status(self) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                """SELECT * FROM index_jobs WHERE state IN ('queued','running')
                   ORDER BY created_at,id LIMIT 1"""
            ).fetchone()
        return {
            "active": row is not None,
            "job": _job(row, reader=True) if row is not None else None,
        }


def _report_counts(report: dict[str, Any]) -> tuple[int, int, int]:
    source_reports = report.get("sources")
    reports: list[Any] = source_reports if isinstance(source_reports, list) else [report]
    discovered = processed = searchable = 0
    for item in reports:
        if not isinstance(item, dict):
            continue
        item_discovered = int(
            item.get("discovered", 0) or int(item.get("indexed", 0)) + int(item.get("unchanged", 0))
        )
        item_processed = int(item.get("indexed", 0)) + int(item.get("unchanged", 0))
        discovered += item_discovered
        processed += item_processed
        searchable += item_processed
    return discovered, processed, searchable


def _job(row: Any, *, reader: bool) -> dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "state": str(row["state"]),
        "phase": str(row["phase"]),
        "active_source": row["source"],
        "discovered": int(row["discovered"]),
        "processed": int(row["processed"]),
        "searchable": int(row["searchable"]),
        "remaining": int(row["remaining"]),
        "embedding_pending": int(row["embedding_pending"]),
        "heartbeat_at": row["heartbeat_at"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }
    if reader:
        result["has_error"] = bool(row["error"])
    else:
        result.update(
            {
                "target": row["target"],
                "reextract": bool(row["reextract"]),
                "full": bool(row["full"]),
                "error": row["error"],
            }
        )
    return result
