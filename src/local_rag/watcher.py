from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer

from .config import Settings
from .indexer import Indexer
from .jobs import write_daemon_heartbeat
from .service import LocalRAG, MultiSourceRAG
from .sources import SourceRecord

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingChange:
    kind: str
    path: Path
    source: Path | None
    ready_at: float


class WatchTarget(Protocol):
    settings: Settings
    indexer: Indexer


class CoalescingEventHandler(FileSystemEventHandler):
    RELEVANT_EVENTS = {"created", "modified", "deleted", "moved", "closed"}

    def __init__(
        self,
        service: WatchTarget,
        stabilize_seconds: float = 0.35,
        max_pending: int = 1024,
    ):
        self.service = service
        self.stabilize_seconds = stabilize_seconds
        self.max_pending = max_pending
        self._pending: OrderedDict[str, PendingChange] = OrderedDict()
        self._lock = threading.Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type not in self.RELEVANT_EVENTS:
            return
        if isinstance(event, FileSystemMovedEvent):
            source = Path(os.fsdecode(event.src_path))
            path = Path(os.fsdecode(event.dest_path))
            kind = "moved"
            if not self._relevant(source) and not self._relevant(path):
                return
        else:
            source = None
            path = Path(os.fsdecode(event.src_path))
            kind = event.event_type
            if not self._relevant(path):
                return
        key = str(path.resolve())
        ready_at = time.monotonic() + self.stabilize_seconds
        with self._lock:
            previous = self._pending.get(key)
            if previous is not None and previous.kind == "moved" and kind != "deleted":
                change = PendingChange("moved", path, previous.source, ready_at)
            else:
                change = PendingChange(kind, path, source, ready_at)
            self._pending[key] = change
            self._pending.move_to_end(key)
            while len(self._pending) > self.max_pending:
                self._pending.popitem(last=False)

    def _relevant(self, path: Path) -> bool:
        resolved = path.resolve()
        return (
            self.service.settings.contains(resolved)
            and not self.service.settings.excluded(resolved)
            and resolved.suffix.lower() in self.service.settings.extensions
        )

    def flush_ready(self, force: bool = False) -> dict[str, object]:
        ready = self.drain_ready(force)
        return self.apply_changes(ready)

    def requeue(self, change: PendingChange, delay: float, now: float | None = None) -> None:
        """Put a change back unless a newer event for the same path arrived meanwhile."""
        key = str(change.path.resolve())
        moment = time.monotonic() if now is None else now
        with self._lock:
            if key not in self._pending:
                self._pending[key] = replace(change, ready_at=moment + delay)

    def drain_ready(self, force: bool = False, now: float | None = None) -> list[PendingChange]:
        now = time.monotonic() if now is None else now
        with self._lock:
            ready = [
                (key, change)
                for key, change in self._pending.items()
                if force or change.ready_at <= now
            ]
            for key, _ in ready:
                self._pending.pop(key, None)
        return [change for _, change in ready]

    def apply_changes(self, ready: list[PendingChange]) -> dict[str, object]:
        paths: list[Path] = []
        changed = 0
        errors: list[str] = []
        for change in ready:
            try:
                if change.kind == "moved" and change.source is not None:
                    changed += int(self.service.indexer.move(change.source, change.path))
                elif change.kind == "deleted":
                    changed += int(self.service.indexer.remove(change.path))
                else:
                    paths.append(change.path)
            except Exception as exc:
                errors.append(f"{change.path}: {exc}")
        indexed: dict[str, Any] = (
            self.service.indexer.index_paths(paths)
            if paths
            else {
                "changed": 0,
                "embedded": 0,
                "warnings": [],
                "errors": [],
            }
        )
        indexed["changed"] = int(indexed["changed"]) + changed
        indexed["errors"] = [*errors, *indexed["errors"]]
        return indexed

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


class WatchService:
    def __init__(self, service: LocalRAG):
        self.service = service
        self.observer = Observer()
        self.handler = CoalescingEventHandler(service)
        self.stop_event = threading.Event()

    def run(self) -> None:
        self.service.scan()
        self.observer.schedule(self.handler, str(self.service.settings.root), recursive=True)
        self.observer.start()
        last_reconcile = time.monotonic()
        try:
            while not self.stop_event.wait(0.1):
                self.handler.flush_ready()
                if time.monotonic() - last_reconcile >= self.service.settings.reconcile_seconds:
                    self.service.scan()
                    last_reconcile = time.monotonic()
        finally:
            self.handler.flush_ready(force=True)
            self.observer.stop()
            self.observer.join()

    def stop(self) -> None:
        self.stop_event.set()


class _SourceFacade:
    def __init__(self, service: MultiSourceRAG, source: SourceRecord):
        self.settings = service.indexer_for(source).settings
        self.indexer = service.indexer_for(source)


class MultiSourceWatchService:
    """One native observer with one coalescing handler per enabled local source.

    This is the serve daemon: it publishes a heartbeat so other processes defer index jobs to
    it, and it executes jobs that those processes left queued.
    """

    HEARTBEAT_SECONDS = 10.0
    RETRY_SECONDS = 1.0
    LOG_THROTTLE_SECONDS = 60.0

    def __init__(self, service: MultiSourceRAG):
        self.service = service
        self.observer = Observer()
        self.handlers: list[tuple[SourceRecord, CoalescingEventHandler]] = []
        self.stop_event = threading.Event()
        self.started_at = datetime.now(UTC)
        self._error_logged: dict[str, float] = {}
        for source in service.registry.list(enabled=True):
            if source.kind != "local":
                continue
            facade = _SourceFacade(service, source)
            handler = CoalescingEventHandler(facade)
            self.handlers.append((source, handler))
            self.observer.schedule(handler, source.locator, recursive=True)

    def heartbeat(self, now: datetime | None = None) -> None:
        write_daemon_heartbeat(self.service.db, self.started_at, now=now)

    def _heartbeat_loop(self) -> None:
        # A separate thread: an inline job may run for minutes and must not look like a dead daemon.
        while not self.stop_event.wait(self.HEARTBEAT_SECONDS):
            try:
                self.heartbeat()
            except Exception:
                continue

    def run(self) -> None:
        self.heartbeat()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="local-rag-daemon-heartbeat", daemon=True
        )
        heartbeat_thread.start()
        self.service.start_index_job("reconcile", background=False, defer_to_daemon=False)
        self.observer.start()
        last_reconcile = time.monotonic()
        try:
            while not self.stop_event.wait(0.1):
                self.tick()
                if time.monotonic() - last_reconcile >= self.service.settings.reconcile_seconds:
                    self.service.start_index_job(
                        "reconcile", background=False, defer_to_daemon=False
                    )
                    last_reconcile = time.monotonic()
        finally:
            for source, handler in self.handlers:
                self._flush_source(source, handler, force=True)
            self.observer.stop()
            self.observer.join()
            self.stop_event.set()
            heartbeat_thread.join(timeout=1)

    def _log_tick_error(self, exc: Exception, now: float | None = None) -> None:
        message = str(exc)
        if isinstance(exc, RuntimeError) and "lease is held by another job" in message:
            # Expected: another process is indexing; the job stays queued for a later tick.
            LOGGER.debug("queued job deferred: %s", message)
            return
        moment = time.monotonic() if now is None else now
        last = self._error_logged.get(message)
        if last is None or moment - last >= self.LOG_THROTTLE_SECONDS:
            self._error_logged[message] = moment
            LOGGER.warning("daemon failed to run queued job: %s", message)

    def tick(self, now: float | None = None) -> None:
        """One scheduler step: claim a queued job, then flush file events if the queue is idle."""
        try:
            self.service.run_next_queued_job()
        except Exception as exc:
            self._log_tick_error(exc)
        if self.service.index_status()["active"]:
            return
        for source, handler in self.handlers:
            self._flush_source(source, handler, now=now)

    def stop(self) -> None:
        self.stop_event.set()

    def _flush_source(
        self,
        source: SourceRecord,
        handler: CoalescingEventHandler,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> None:
        for change in handler.drain_ready(force, now):
            targets = [change.path]
            if change.kind == "moved" and change.source is not None:
                targets.insert(0, change.source)
            rejected = False
            for target in targets:
                job = self.service.start_index_job(
                    "reconcile",
                    source.name,
                    str(target),
                    background=False,
                    defer_to_daemon=False,
                )
                if job["state"] == "rejected":
                    rejected = True
            if rejected and not force:
                # Another job is active; keep the event and retry on a later tick.
                handler.requeue(change, self.RETRY_SECONDS, now)
