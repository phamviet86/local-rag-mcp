import threading
import time
from pathlib import Path
from typing import Dict

from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers.polling import PollingObserver

from .service import LocalRAG


class IndexEventHandler(FileSystemEventHandler):
    def __init__(self, service: LocalRAG, debounce_seconds: float = 0.25):
        self.service = service
        self.debounce_seconds = debounce_seconds
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        key = f"{event.event_type}:{event.src_path}:{getattr(event, 'dest_path', '')}"
        with self._lock:
            now = time.monotonic()
            if now - self._seen.get(key, 0) < self.debounce_seconds:
                return
            self._seen[key] = now
        job = self.service.db.start_job(f"watch:{event.event_type}", event.src_path)
        try:
            if isinstance(event, FileSystemMovedEvent):
                changed = self.service.indexer.move(Path(event.src_path), Path(event.dest_path))
            elif event.event_type == "deleted":
                changed = self.service.indexer.remove(Path(event.src_path))
            else:
                changed = self.service.indexer.index_file(Path(event.src_path))
            self.service.db.finish_job(job, "completed", {"changed": changed})
        except Exception as exc:
            self.service.db.finish_job(job, "failed", {"error": str(exc)})


class WatchService:
    def __init__(self, service: LocalRAG):
        self.service = service
        self.observer = PollingObserver(timeout=0.1)
        self.stop_event = threading.Event()

    def run(self) -> None:
        self.service.scan()
        self.observer.schedule(
            IndexEventHandler(self.service), str(self.service.settings.root), recursive=True
        )
        self.observer.start()
        try:
            while not self.stop_event.wait(self.service.settings.reconcile_seconds):
                self.service.scan()
        finally:
            self.observer.stop()
            self.observer.join()

    def stop(self) -> None:
        self.stop_event.set()
