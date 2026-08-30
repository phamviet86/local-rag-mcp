from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer

from .config import Settings
from .indexer import Indexer
from .service import LocalRAG, MultiSourceRAG
from .sources import SourceRecord


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
        now = time.monotonic()
        with self._lock:
            ready = [
                (key, change)
                for key, change in self._pending.items()
                if force or change.ready_at <= now
            ]
            for key, _ in ready:
                self._pending.pop(key, None)
        paths: list[Path] = []
        changed = 0
        errors: list[str] = []
        for _, change in ready:
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
    """One native observer with one coalescing handler per enabled local source."""

    def __init__(self, service: MultiSourceRAG):
        self.service = service
        self.observer = Observer()
        self.handlers: list[CoalescingEventHandler] = []
        self.stop_event = threading.Event()
        for source in service.registry.list(enabled=True):
            if source.kind != "local":
                continue
            facade = _SourceFacade(service, source)
            handler = CoalescingEventHandler(facade)
            self.handlers.append(handler)
            self.observer.schedule(handler, source.locator, recursive=True)

    def run(self) -> None:
        self.service.reconcile()
        self.observer.start()
        last_reconcile = time.monotonic()
        try:
            while not self.stop_event.wait(0.1):
                for handler in self.handlers:
                    handler.flush_ready()
                if time.monotonic() - last_reconcile >= self.service.settings.reconcile_seconds:
                    self.service.reconcile()
                    last_reconcile = time.monotonic()
        finally:
            for handler in self.handlers:
                handler.flush_ready(force=True)
            self.observer.stop()
            self.observer.join()

    def stop(self) -> None:
        self.stop_event.set()
