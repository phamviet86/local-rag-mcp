from __future__ import annotations

import shutil
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .embeddings import (
    EmbeddingProvider,
    cache_identity,
    configured_provider,
    provider_readiness,
    public_identity,
)
from .extract import Extractor
from .indexer import Indexer
from .jobs import IndexJobManager
from .ocr_runtime import OCRRuntimeManager
from .search import SearchEngine
from .sources import SourceRecord, SourceRegistry, source_settings


def _readiness_error(code: str, message: str, *commands: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "actions": [{"command": command} for command in commands],
    }


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
        self.extractor = Extractor(
            self.ocr_runtime,
            ocr_enabled=settings.ocr_mode != "no-ocr",
            ocr_offline=settings.ocr_mode == "full",
        )
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
        self.extractor = Extractor(
            self.ocr_runtime,
            ocr_enabled=settings.ocr_mode != "no-ocr",
            ocr_offline=settings.ocr_mode == "full",
        )
        self.search_engine = SearchEngine(settings, self.db, self.embeddings)
        self.drive_backend_factory = drive_backend_factory
        self.jobs = IndexJobManager(self.db)

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

    def source_summary(self) -> dict[str, Any]:
        sources = self.sources()
        result: dict[str, Any] = {
            "sources": sources,
            "count": len(sources),
            "enabled_count": sum(bool(source["enabled"]) for source in sources),
        }
        if not sources:
            result["error"] = _readiness_error(
                "no_sources",
                "No sources are configured; choose a local folder or Google Drive root.",
                "local-rag-mcp source add-local NAME FOLDER",
                "local-rag-mcp auth-google --client-secret FILE --token-file FILE",
                "local-rag-mcp source add-drive NAME ROOT_ID --account LABEL --token-file FILE",
            )
        return result

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
        progress: Callable[[dict[str, int | str]], None] | None = None,
    ) -> dict[str, Any]:
        selected = [self.registry.get(source)] if source else self.registry.list(enabled=True)
        if not selected:
            return {
                "sources": [],
                "errors": [],
                "error": _readiness_error(
                    "no_enabled_sources",
                    "No enabled source can be reconciled.",
                    "local-rag-mcp source list",
                    "local-rag-mcp source add-local NAME FOLDER",
                ),
            }
        reports, errors = [], []
        progress_by_source: dict[str, dict[str, int]] = {}

        def source_progress(record: SourceRecord, update: dict[str, int | str]) -> None:
            state = progress_by_source.setdefault(
                record.id,
                {
                    "discovered": 0,
                    "processed": 0,
                    "searchable": 0,
                    "remaining": 0,
                    "embedding_pending": 0,
                },
            )
            for key in state:
                if key in update:
                    state[key] = int(update[key])
            if progress is not None:
                progress(
                    {
                        "phase": str(update.get("phase", "indexing")),
                        "active_source": record.name,
                        **{
                            key: sum(item[key] for item in progress_by_source.values())
                            for key in state
                        },
                    }
                )

        for record in selected:
            if not record.enabled and source is None:
                continue
            try:
                if record.kind == "local":
                    indexer = self.indexer_for(record)
                    local_settings = indexer.settings
                    path = local_settings.scope(target) if target else None
                    report = indexer.reconcile(
                        path,
                        force_index=force_index,
                        reextract=reextract,
                        progress=partial(source_progress, record),
                    )
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
                            progress=partial(source_progress, record),
                        )
                    finally:
                        backend.close()
                reports.append(report)
            except Exception as exc:
                if record.kind == "google_drive":
                    from .coverage import source_failure

                    source_failure(self.db, record.id, type(exc).__name__)
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
        if not self.registry.list(enabled=True):
            return {
                "query": query,
                "mode": mode,
                "effective_mode": "unavailable",
                "results": [],
                "warnings": ["No enabled sources are configured."],
                "index_status": self.index_status(),
                "error": _readiness_error(
                    "no_enabled_sources",
                    "Search requires at least one enabled source.",
                    "local-rag-mcp source list",
                    "local-rag-mcp source add-local NAME FOLDER",
                ),
            }
        result = self.search_engine.search(query, limit, folder, mode, source)
        result["index_status"] = self.index_status()
        result["coverage"] = self.index_coverage(source, folder)
        if result["coverage"]["notice"]:
            result["warnings"].append(result["coverage"]["notice"])
        return result

    def index_coverage(
        self,
        source: str | None = None,
        folder: str | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> dict[str, Any]:
        from .coverage import index_coverage

        normalized = self.search_engine._relative_scope(folder)
        return index_coverage(self.db, source, normalized, offset, limit)

    def read(
        self,
        path: str,
        source: str | None = None,
        start: int = 0,
        length: int = 12000,
    ) -> dict[str, Any]:
        return self.search_engine.read(path, start, length, source)

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            **self.db.stats(),
            "home": str(self.settings.home),
            "database": str(self.settings.database),
            "embedding_provider": self.settings.embedding_provider,
            "embedding_model": self.settings.embedding_model,
            "embedding_identity": public_identity(self.embeddings),
            "ocr_runtime_installed": self.ocr_runtime.configure(),
            "ocr_mode": self.settings.ocr_mode,
            "reconcile_seconds": self.settings.reconcile_seconds,
            "source_status": self.sources(),
            "index_status": self.index_status(),
        }
        if not result["enabled_sources"]:
            result["error"] = _readiness_error(
                "no_enabled_sources",
                "No enabled sources are configured.",
                "local-rag-mcp source add-local NAME FOLDER",
            )
        return result

    def index_status(self) -> dict[str, Any]:
        return self.jobs.index_status()

    def job_status(self, job_id: str, *, reader: bool = True) -> dict[str, Any]:
        return self.jobs.status(job_id, reader=reader)

    def list_jobs(self, *, reader: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        return self.jobs.list(reader=reader, limit=limit)

    def enqueue_index_job(
        self,
        kind: str,
        source: str | None = None,
        target: str | None = None,
        *,
        reextract: bool = False,
        full: bool = False,
    ) -> dict[str, Any]:
        return self.jobs.enqueue(kind, source, target, reextract=reextract, full=full)

    def run_index_job(self, job_id: str, *, background: bool = False) -> dict[str, Any]:
        def execute(job: dict[str, Any], phase: Callable[[str], None]) -> dict[str, Any]:
            phase("reconciling")
            current = {
                "discovered": 0,
                "processed": 0,
                "searchable": 0,
                "remaining": 0,
                "embedding_pending": 0,
            }

            def update_progress(update: dict[str, int | str]) -> None:
                if "phase" in update:
                    phase(str(update["phase"]))
                for key in current:
                    if key in update:
                        current[key] = int(update[key])
                self.jobs.progress(str(job["id"]), **current)

            report = self.reconcile(
                job.get("active_source"),
                job.get("target"),
                force_index=job["kind"] == "reindex",
                reextract=bool(job.get("reextract", False)),
                full=bool(job.get("full", False)),
                progress=update_progress,
            )
            source_errors = [
                error
                for item in report.get("sources", [])
                if isinstance(item, dict)
                for error in item.get("errors", [])
            ]
            errors = [*report.get("errors", []), *source_errors]
            if errors:
                raise RuntimeError("; ".join(str(error) for error in errors))
            return report

        if background:
            return self.jobs.start_background(job_id, execute, self._embedding_pending_count)
        return self.jobs.run(job_id, execute, self._embedding_pending_count)

    def start_index_job(
        self,
        kind: str,
        source: str | None = None,
        target: str | None = None,
        *,
        reextract: bool = False,
        full: bool = False,
        background: bool = True,
    ) -> dict[str, Any]:
        job = self.enqueue_index_job(kind, source, target, reextract=reextract, full=full)
        if job["state"] in {"rejected", "complete", "error", "running"}:
            return job
        return self.run_index_job(str(job["id"]), background=background)

    def _embedding_pending_count(self) -> int:
        if self.embeddings is None:
            return 0
        provider, model = cache_identity(self.embeddings)
        with self.db.connect() as connection:
            return int(
                connection.execute(
                    """SELECT count(DISTINCT c.chunk_hash) FROM chunks c
                       LEFT JOIN vectors v ON v.chunk_hash=c.chunk_hash
                         AND v.provider=? AND v.model=? WHERE v.chunk_hash IS NULL""",
                    (provider, model),
                ).fetchone()[0]
            )

    def doctor(self) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        actions: list[dict[str, str]] = []
        database_ok = False
        try:
            with self.db.connect() as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                connection.execute("SELECT count(*) FROM chunks_fts").fetchone()
            database_ok = integrity == "ok"
            checks["database"] = {
                "status": "ok" if database_ok else "error",
                "message": "SQLite integrity and FTS5 are available" if database_ok else integrity,
            }
        except Exception as exc:
            checks["database"] = {"status": "error", "message": f"database/FTS check failed: {exc}"}
            actions.append({"command": "local-rag-mcp reindex --all"})

        source_rows = self.registry.list()
        enabled = [source for source in source_rows if source.enabled]
        checks["sources"] = {
            "status": "ok" if enabled else "error",
            "message": f"{len(enabled)} of {len(source_rows)} sources enabled",
        }
        if not enabled:
            actions.append({"command": "local-rag-mcp source add-local NAME FOLDER"})

        ocr_installed = self.ocr_runtime.configure()
        ocr_status = "ok" if self.settings.ocr_mode == "full" and ocr_installed else "warning"
        if self.settings.ocr_mode == "full" and not ocr_installed:
            ocr_status = "error"
            actions.append({"command": "local-rag-mcp setup --full"})
        elif self.settings.ocr_mode != "full":
            actions.append({"command": "local-rag-mcp setup --full"})
        checks["ocr"] = {
            "status": ocr_status,
            "mode": self.settings.ocr_mode,
            "available": ocr_installed and self.settings.ocr_mode != "no-ocr",
            "message": (
                "local OCR runtime and model cache are available"
                if ocr_status == "ok"
                else "non-OCR indexing remains available; OCR-routed pages enter review"
            ),
        }

        embedding_available, embedding_message, embedding_action = provider_readiness(
            self.settings, self.embeddings
        )
        embedding_status = "ok" if embedding_available else "warning"
        checks["embeddings"] = {
            "status": embedding_status,
            "available": embedding_available,
            "provider": self.settings.embedding_provider,
            "identity": public_identity(self.embeddings),
            "message": embedding_message,
        }
        if embedding_action == "install_local_embeddings":
            actions.append({"command": "pip install 'phamviet-local-rag-mcp[local-embeddings]'"})
        elif embedding_action == "configure_openai_key":
            actions.append({"command": "export LOCAL_RAG_MCP_OPENAI_API_KEY='...'"})
        elif embedding_action == "configure_embedding_model":
            actions.append({"command": "export LOCAL_RAG_MCP_EMBEDDING_MODEL='MODEL_ID'"})

        with self.db.connect() as connection:
            review_rows = connection.execute(
                "SELECT reason,count(*) count FROM reviews WHERE status='open' GROUP BY reason"
            ).fetchall()
            sync_rows = connection.execute(
                """SELECT name,kind,last_sync_at,last_error FROM sources
                   WHERE enabled=1 ORDER BY name"""
            ).fetchall()
        review_counts = {str(row["reason"]): int(row["count"]) for row in review_rows}
        review_total = sum(review_counts.values())
        checks["reviews"] = {
            "status": "warning" if review_total else "ok",
            "open": review_total,
            "reasons": review_counts,
            "message": "open review items require attention" if review_total else "no open reviews",
        }
        if review_total:
            actions.append({"command": "local-rag-mcp review list"})
        sync_errors = [str(row["name"]) for row in sync_rows if row["last_error"]]
        checks["sync"] = {
            "status": "error" if sync_errors else ("ok" if sync_rows else "warning"),
            "sources_with_errors": sync_errors,
            "sources": [
                {
                    "name": row["name"],
                    "kind": row["kind"],
                    "last_sync_at": row["last_sync_at"],
                    "error": row["last_error"],
                }
                for row in sync_rows
            ],
            "message": "sync errors detected" if sync_errors else "no recorded sync errors",
        }
        blocking = not database_ok or not enabled or ocr_status == "error" or bool(sync_errors)
        degraded = any(check["status"] == "warning" for check in checks.values())
        return {
            "schema_version": 1,
            "ok": not blocking,
            "status": "blocked" if blocking else ("degraded" if degraded else "ready"),
            "capabilities": {
                "full_text": database_ok and bool(enabled),
                "semantic": embedding_available and bool(enabled),
                "ocr": ocr_installed and self.settings.ocr_mode != "no-ocr",
            },
            "checks": checks,
            "actions": actions,
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
