"""Per-source sync schedule for the serve daemon (poll and periodic full reconcile)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .jobs import iso_z, parse_utc
from .sources import LAST_FULL_KEY, SourceRecord, full_reconcile_seconds, poll_seconds

if TYPE_CHECKING:
    from .service import MultiSourceRAG


class SyncScheduler:
    """Decides which enabled source is due and enqueues at most one reconcile job per call.

    Time is read from an injectable wall clock so tests never sleep. A source is due when its
    newest reference (last sync attempt, newest whole-source reconcile job, or scheduler start
    for a never-synced source) is older than its poll interval. A Drive source additionally gets
    ``full=True`` once ``full_reconcile_seconds`` have passed since the last recorded full scan.
    Local reconciles always walk the whole tree, so they are never ``full``.
    """

    def __init__(
        self,
        service: MultiSourceRAG,
        clock: Callable[[], datetime] | None = None,
    ):
        self.service = service
        self.clock = clock or (lambda: service.clock())
        self.started_at = self.clock()

    def _reference(self, source: SourceRecord) -> datetime:
        moments: list[datetime] = []
        stamp = iso_z(source.last_sync_at)
        if stamp:
            moments.append(parse_utc(stamp))
        else:
            moments.append(self.started_at)
        latest = self.service.jobs.latest_reconcile([source.id, source.name])
        if latest is not None:
            moments.append(parse_utc(latest["created_at"]))
        return max(moments)

    def _last_full(self, source: SourceRecord) -> datetime:
        stamp = self.service.db.sync_state(source.id, LAST_FULL_KEY)
        if isinstance(stamp, str) and stamp:
            return parse_utc(stamp)
        # Never recorded: age from source creation so restarts cannot postpone the full scan.
        return parse_utc(source.created_at) if source.created_at else self.started_at

    def due(self, source: SourceRecord, now: datetime | None = None) -> str | None:
        """Return "full", "incremental" or None for one source."""
        moment = now or self.clock()
        if source.kind == "google_drive":
            elapsed_full = (moment - self._last_full(source)).total_seconds()
            if elapsed_full >= full_reconcile_seconds(source):
                return "full"
        elapsed = (moment - self._reference(source)).total_seconds()
        if elapsed >= poll_seconds(self.service.settings, source):
            return "incremental"
        return None

    def run_once(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Enqueue the most overdue source's reconcile when no job is active.

        The daemon claims the queued job itself on its next tick. Nothing is written unless a
        source is due and the queue is idle; a later pass retries after a skipped enqueue.
        """
        if self.service.index_status()["active"]:
            return None
        moment = now or self.clock()
        candidates: list[tuple[datetime, SourceRecord, str]] = []
        for source in self.service.registry.list(enabled=True):
            kind = self.due(source, moment)
            if kind is not None:
                candidates.append((self._reference(source), source, kind))
        if not candidates:
            return None
        _, source, kind = min(candidates, key=lambda item: (item[0], item[1].name))
        job = self.service.enqueue_index_job("reconcile", source.name, full=kind == "full")
        return job if job["state"] == "queued" else None
