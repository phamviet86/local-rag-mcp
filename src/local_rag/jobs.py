from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .db import Database

ACTIVE_STATES = ("queued", "running")
QUEUED_EXPIRY_SQL = "-20 minutes"
DAEMON_MAX_AGE_SECONDS = 30.0
SWEEP_INTERVAL_SECONDS = 60.0
_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_ISO_Z)


def write_daemon_heartbeat(
    db: Database, started_at: datetime, *, now: datetime | None = None
) -> None:
    """Upsert the single serve-daemon heartbeat row (UTC ISO 8601 with Z)."""
    moment = now or datetime.now(UTC)
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO daemon_heartbeat(singleton,pid,hostname,started_at,heartbeat_at)
               VALUES(1,?,?,?,?)
               ON CONFLICT(singleton) DO UPDATE SET pid=excluded.pid,
                 hostname=excluded.hostname,started_at=excluded.started_at,
                 heartbeat_at=excluded.heartbeat_at""",
            (os.getpid(), socket.gethostname(), _utc_text(started_at), _utc_text(moment)),
        )


def daemon_info(
    db: Database,
    max_age: float = DAEMON_MAX_AGE_SECONDS,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the live daemon record, or None when no heartbeat is fresh enough."""
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM daemon_heartbeat WHERE singleton=1").fetchone()
    if row is None:
        return None
    try:
        beat = datetime.strptime(str(row["heartbeat_at"]), _ISO_Z).replace(tzinfo=UTC)
    except ValueError:
        return None
    moment = now or datetime.now(UTC)
    if (moment - beat).total_seconds() > max_age:
        return None
    return {
        "pid": int(row["pid"]),
        "hostname": str(row["hostname"]),
        "started_at": str(row["started_at"]),
        "heartbeat_at": str(row["heartbeat_at"]),
    }


def daemon_alive(
    db: Database,
    max_age: float = DAEMON_MAX_AGE_SECONDS,
    *,
    now: datetime | None = None,
) -> bool:
    return daemon_info(db, max_age, now=now) is not None


class IndexJobManager:
    """Durable single-writer coordination; committed WAL readers remain independent."""

    def __init__(self, database: Database):
        self.db = database
        self._threads: dict[str, threading.Thread] = {}
        self._last_sweep = 0.0

    def enqueue(
        self,
        kind: str,
        source: str | None = None,
        target: str | None = None,
        *,
        reextract: bool = False,
        full: bool = False,
    ) -> dict[str, Any]:
        if kind not in {"reconcile", "reindex", "relocate"}:
            raise ValueError("job kind must be reconcile, reindex or relocate")
        identifier = uuid.uuid4().hex
        with self.db.transaction() as connection:
            self._expire_stale(connection)
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

    @staticmethod
    def _expire_stale(connection: Any) -> None:
        connection.execute(
            """UPDATE index_jobs SET state='error',phase='error',
               error='writer heartbeat expired',completed_at=CURRENT_TIMESTAMP
               WHERE state='running' AND heartbeat_at < datetime('now','-20 minutes')"""
        )
        connection.execute(
            """UPDATE index_jobs SET state='error',phase='error',
               error='queued job was never started',completed_at=CURRENT_TIMESTAMP
               WHERE state='queued' AND created_at < datetime('now',?)""",
            (QUEUED_EXPIRY_SQL,),
        )
        connection.execute(
            """UPDATE index_writer_lease SET job_id=NULL
               WHERE job_id IN (SELECT id FROM index_jobs WHERE state='error')"""
        )

    def next_queued(self, *, monotonic: Callable[[], float] = time.monotonic) -> str | None:
        """Oldest queued job id when no job is running; None otherwise.

        The idle path is a read-only SELECT. The write transaction (with the expiry sweep) is
        taken only when a queued job exists or the sweep is due (at most every 60 seconds).
        """
        now = monotonic()
        with self.db.connect() as connection:
            queued = connection.execute(
                "SELECT 1 FROM index_jobs WHERE state='queued' LIMIT 1"
            ).fetchone()
        if queued is None and now - self._last_sweep < SWEEP_INTERVAL_SECONDS:
            return None
        self._last_sweep = now
        with self.db.transaction() as connection:
            self._expire_stale(connection)
            running = connection.execute(
                "SELECT 1 FROM index_jobs WHERE state='running' LIMIT 1"
            ).fetchone()
            if running is not None:
                return None
            row = connection.execute(
                """SELECT id FROM index_jobs WHERE state='queued'
                   ORDER BY created_at,id LIMIT 1"""
            ).fetchone()
        return str(row["id"]) if row is not None else None

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
            if row["state"] != "queued":
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
