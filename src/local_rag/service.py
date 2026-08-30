from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .embeddings import EmbeddingProvider, configured_provider, public_identity
from .extract import Extractor
from .indexer import Indexer
from .ocr_runtime import OCRRuntimeManager
from .search import SearchEngine
from .sources import SourceRecord, SourceRegistry, source_settings


class LocalRAG:
    def __init__(self, settings: Settings, embeddings: EmbeddingProvider | None = None):
        settings.initialize_layout()
        self.settings = settings
        self.db = Database(settings.database)
        self.db.migrate()
        try:
            source = self.db.source("legacy")
            if not source["locator"]:
                with self.db.connect() as connection:
                    connection.execute(
                        "UPDATE sources SET locator=? WHERE id='legacy'", (str(settings.root),)
                    )
        except ValueError:
            source = self.db.add_source("default", "local", str(settings.root), source_id="legacy")
        self.ocr_runtime = OCRRuntimeManager(
            settings.runtime_dir, settings.model_dir / "pp-ocrv6-small"
        )
        self.embeddings = configured_provider(settings) if embeddings is None else embeddings
        self.extractor = Extractor(self.ocr_runtime)
        self.indexer = Indexer(
            settings,
            self.db,
            self.extractor,
            self.embeddings,
            source_id=str(source["id"]),
            source_name=str(source["name"]),
        )
        self.search_engine = SearchEngine(settings, self.db, self.embeddings)

    def scan(
        self,
        target: str | None = None,
        force_index: bool = False,
        reextract: bool = False,
    ) -> dict[str, Any]:
        path = self.settings.scope(target) if target else None
        return self.indexer.reconcile(path, force_index=force_index, reextract=reextract)

    def search(
        self,
        query: str,
        limit: int = 8,
        scope: str | None = None,
        mode: str = "hybrid",
    ) -> dict[str, Any]:
        return self.search_engine.search(query, limit, scope, mode, self.indexer.source_id)

    def read(self, path: str, start: int = 0, length: int = 12000) -> dict[str, Any]:
        return self.search_engine.read(path, start, length, self.indexer.source_id)

    def status(self) -> dict[str, Any]:
        return {
            **self.db.stats(),
            "root": str(self.settings.root),
            "home": str(self.settings.home),
            "database": str(self.settings.database),
            "embedding_provider": self.settings.embedding_provider,
            "embedding_model": self.settings.embedding_model,
            "embedding_identity": public_identity(self.embeddings),
            "ocr_runtime_installed": self.ocr_runtime.configure(),
        }

    def reviews(self, status: str = "open") -> list[dict[str, Any]]:
        return self.db.list_reviews(status)

    def resolve_review(self, review_id: int, resolution: str) -> dict[str, Any]:
        self.db.resolve_review(review_id, resolution)
        return {"id": review_id, "status": "resolved"}

    def correct_review(
        self,
        review_id: int,
        corrected_text: str,
        evidence: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return self.indexer.correct_review(review_id, corrected_text, evidence, actor)

    def metadata(self, path: str) -> dict[str, Any]:
        return self.db.metadata(path)

    def add_metadata(
        self, path: str, key: str, value: Any, evidence: list[dict[str, Any]], actor: str
    ) -> dict[str, Any]:
        return {"id": self.db.add_metadata(path, key, value, evidence, actor)}

    def add_relationship(
        self,
        source: str,
        target: str,
        relation: str,
        evidence: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return {"id": self.db.add_relationship(source, target, relation, evidence, actor)}


class MultiSourceRAG:
    """One shared SQLite engine coordinating independent local and Drive sources."""

    def __init__(
        self,
        settings: Settings,
        embeddings: EmbeddingProvider | None = None,
        drive_backend_factory: Callable[[SourceRecord], Any] | None = None,
    ):
        settings.initialize_layout()
        self.settings = settings
        self.db = Database(settings.database)
        self.db.migrate()
        self.registry = SourceRegistry(settings, self.db)
        self._migrate_legacy_root()
        self.ocr_runtime = OCRRuntimeManager(
            settings.runtime_dir, settings.model_dir / "pp-ocrv6-small"
        )
        self.embeddings = configured_provider(settings) if embeddings is None else embeddings
        self.extractor = Extractor(self.ocr_runtime)
        self.search_engine = SearchEngine(settings, self.db, self.embeddings)
        self.drive_backend_factory = drive_backend_factory

    def _migrate_legacy_root(self) -> None:
        sources = self.registry.list()
        legacy_root = self.settings.root
        if not sources and legacy_root.is_dir() and legacy_root != self.settings.home:
            self.db.add_source("default", "local", str(legacy_root), source_id="legacy")
        elif sources:
            for source in sources:
                if source.id == "legacy" and not source.locator:
                    with self.db.connect() as connection:
                        connection.execute(
                            "UPDATE sources SET locator=? WHERE id='legacy'",
                            (str(legacy_root),),
                        )

    def indexer_for(self, source: SourceRecord) -> Indexer:
        return Indexer(
            source_settings(self.settings, source),
            self.db,
            self.extractor,
            self.embeddings,
            source_id=source.id,
            source_name=source.name,
        )

    def add_local_source(
        self, name: str, root: Path, exclusions: list[str] | None = None
    ) -> dict[str, Any]:
        return self.registry.public(self.registry.add_local(name, root, exclusions))

    def add_drive_source(
        self,
        name: str,
        root_id: str,
        account: str,
        token_file: Path,
        shared_drive_id: str = "",
        exclusions: list[str] | None = None,
    ) -> dict[str, Any]:
        source = self.registry.add_drive(
            name, root_id, account, token_file, shared_drive_id, exclusions
        )
        return self.registry.public(source)

    def sources(self) -> list[dict[str, Any]]:
        return [self.registry.public(source) for source in self.registry.list()]

    def enable_source(self, value: str, enabled: bool) -> dict[str, Any]:
        return self.registry.public(self.registry.enable(value, enabled))

    def remove_source(self, value: str) -> dict[str, Any]:
        source = self.registry.get(value)
        with self.db.connect() as connection:
            artifacts = {
                str(path)
                for row in connection.execute(
                    """SELECT artifact_path,effective_artifact_path FROM documents
                       WHERE source_id=?""",
                    (source.id,),
                )
                for path in (row["artifact_path"], row["effective_artifact_path"])
                if path
            }
            artifacts.update(
                str(row["artifact_path"])
                for row in connection.execute(
                    """SELECT rr.artifact_path FROM review_revisions rr
                       JOIN documents d ON d.id=rr.document_id WHERE d.source_id=?""",
                    (source.id,),
                )
                if row["artifact_path"]
            )
        result = self.db.purge_source(source.id)
        cache = self.settings.cache_dir / "sources" / source.id
        if cache.exists():
            shutil.rmtree(cache)
        removed_artifacts = 0
        with self.db.connect() as connection:
            referenced = {
                str(path)
                for row in connection.execute(
                    "SELECT artifact_path,effective_artifact_path FROM documents"
                )
                for path in (row["artifact_path"], row["effective_artifact_path"])
                if path
            }
        for value in artifacts - referenced:
            artifact = Path(value).resolve()
            if self.settings.home == artifact or self.settings.home in artifact.parents:
                artifact.unlink(missing_ok=True)
                removed_artifacts += 1
        result.update({"source_files_removed": False, "cache_removed": not cache.exists()})
        result["artifacts_removed"] = removed_artifacts
        return result

    def reconcile(
        self,
        source: str | None = None,
        target: str | None = None,
        force_index: bool = False,
        reextract: bool = False,
        full: bool = False,
    ) -> dict[str, Any]:
        selected = [self.registry.get(source)] if source else self.registry.list(enabled=True)
        reports, errors = [], []
        for record in selected:
            if not record.enabled and source is None:
                continue
            try:
                if record.kind == "local":
                    indexer = self.indexer_for(record)
                    local_settings = indexer.settings
                    path = local_settings.scope(target) if target else None
                    report = indexer.reconcile(path, force_index=force_index, reextract=reextract)
                    report.update({"source": record.name, "kind": record.kind})
                else:
                    from .drive import DriveAdapter, GoogleDriveBackend

                    backend = (
                        self.drive_backend_factory(record)
                        if self.drive_backend_factory
                        else GoogleDriveBackend(record)
                    )
                    try:
                        report = DriveAdapter(self, record, backend).sync(
                            force_index=force_index,
                            reextract=reextract,
                            full=full,
                            target=target,
                        )
                    finally:
                        backend.close()
                reports.append(report)
            except Exception as exc:
                errors.append(f"{record.name}: {exc}")
        return {"sources": reports, "errors": errors}

    def search(
        self,
        query: str,
        limit: int = 8,
        source: str | None = None,
        folder: str | None = None,
        mode: str = "hybrid",
    ) -> dict[str, Any]:
        return self.search_engine.search(query, limit, folder, mode, source)

    def read(
        self,
        path: str,
        source: str | None = None,
        start: int = 0,
        length: int = 12000,
    ) -> dict[str, Any]:
        return self.search_engine.read(path, start, length, source)

    def status(self) -> dict[str, Any]:
        return {
            **self.db.stats(),
            "home": str(self.settings.home),
            "database": str(self.settings.database),
            "embedding_provider": self.settings.embedding_provider,
            "embedding_model": self.settings.embedding_model,
            "embedding_identity": public_identity(self.embeddings),
            "ocr_runtime_installed": self.ocr_runtime.configure(),
            "source_status": self.sources(),
        }

    def reviews(self, status: str = "open") -> list[dict[str, Any]]:
        return self.db.list_reviews(status)

    def resolve_review(self, review_id: int, resolution: str) -> dict[str, Any]:
        self.db.resolve_review(review_id, resolution)
        return {"id": review_id, "status": "resolved"}

    def correct_review(
        self,
        review_id: int,
        corrected_text: str,
        evidence: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        review = self.db.review(review_id)
        source = self.registry.get(str(review["source_id"]))
        return self.indexer_for(source).correct_review(review_id, corrected_text, evidence, actor)

    def metadata(self, path: str, source: str | None = None) -> dict[str, Any]:
        document = self.db.resolve_document(path, source)
        return self.db.metadata(str(document["path"]))

    def add_metadata(
        self,
        path: str,
        key: str,
        value: Any,
        evidence: list[dict[str, Any]],
        actor: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        document = self.db.resolve_document(path, source)
        return {"id": self.db.add_metadata(str(document["path"]), key, value, evidence, actor)}

    def add_relationship(
        self,
        source_path: str,
        target_path: str,
        relation: str,
        evidence: list[dict[str, Any]],
        actor: str,
        source_source: str | None = None,
        target_source: str | None = None,
    ) -> dict[str, Any]:
        source_document = self.db.resolve_document(source_path, source_source)
        target_document = self.db.resolve_document(target_path, target_source)
        return {
            "id": self.db.add_relationship(
                str(source_document["path"]),
                str(target_document["path"]),
                relation,
                evidence,
                actor,
            )
        }
