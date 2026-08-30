from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .embeddings import EmbeddingProvider, cache_identity
from .extract import Extractor
from .models import ChunkRecord, ExtractedDocument, SourceSpan

ProgressCallback = Callable[[dict[str, int | str]], None]


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    size: int
    modified_ns: int


@dataclass(frozen=True)
class PreparedDocument:
    snapshot: FileSnapshot
    content_hash: str
    extracted: ExtractedDocument
    artifact_path: Path
    chunks: Sequence[ChunkRecord]
    automatic: dict[str, Any]
    preserve_derived_state: bool = False
    relative_path: str | None = None
    external_id: str | None = None
    source_version: str = ""
    source_url: str = ""


class Indexer:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        extractor: Extractor,
        embeddings: EmbeddingProvider | None = None,
        embedding_batch_size: int = 128,
        source_id: str = "legacy",
        source_name: str = "default",
    ):
        self.settings = settings
        self.db = database
        self.extractor = extractor
        self.embeddings = embeddings
        self.embedding_batch_size = embedding_batch_size
        self.source_id = source_id
        self.source_name = source_name

    def files(self, target: Path | None = None) -> Iterator[FileSnapshot]:
        base = (target or self.settings.root).resolve()
        if not self.settings.contains(base) or self.settings.excluded(base):
            raise ValueError("target is outside the configured root or excluded")
        if base.is_file():
            snapshot = self.snapshot(base)
            if snapshot is not None:
                yield snapshot
            return
        yield from self._walk(base)

    def _walk(self, directory: Path) -> Iterator[FileSnapshot]:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return
        for entry in entries:
            candidate = Path(entry.path)
            try:
                if entry.is_symlink():
                    if entry.is_file(follow_symlinks=True):
                        snapshot = self.snapshot(candidate)
                        if snapshot is not None:
                            yield snapshot
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not self.settings.excluded(candidate):
                        yield from self._walk(candidate)
                elif entry.is_file(follow_symlinks=False):
                    snapshot = self.snapshot(candidate)
                    if snapshot is not None:
                        yield snapshot
            except (FileNotFoundError, PermissionError, OSError):
                continue

    def snapshot(self, path: Path) -> FileSnapshot | None:
        resolved = path.resolve()
        if not self.settings.accepts(resolved) or not resolved.is_file():
            return None
        stat = resolved.stat()
        return FileSnapshot(resolved, stat.st_size, stat.st_mtime_ns)

    def reconcile(
        self,
        target: Path | None = None,
        force_index: bool = False,
        reextract: bool = False,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "discovered": 0,
            "indexed": 0,
            "unchanged": 0,
            "stat_refreshed": 0,
            "removed": 0,
            "embedded": 0,
            "warnings": [],
            "errors": [],
        }
        rows = self.db.document_snapshot(self.source_id)
        documents = {row["path"]: row for row in rows}
        present: set[str] = set()
        prepared: list[PreparedDocument] = []
        stat_updates: list[tuple[int, FileSnapshot, dict[str, Any]]] = []
        snapshots = list(self.files(target))
        report["discovered"] = len(snapshots)
        processed = searchable = 0
        _progress(progress, "discovering", len(snapshots), processed, searchable, len(snapshots))
        for snapshot in snapshots:
            present.add(str(snapshot.path))
            current = documents.get(str(snapshot.path))
            try:
                outcome = self._classify(snapshot, current, force_index, reextract)
                if outcome == "unchanged":
                    report["unchanged"] += 1
                elif isinstance(outcome, tuple):
                    stat_updates.append(outcome)
                    report["unchanged"] += 1
                    report["stat_refreshed"] += 1
                else:
                    prepared.append(outcome)
                if current is not None:
                    searchable += 1
            except Exception as exc:
                report["errors"].append(f"{snapshot.path}: {exc}")
            processed += 1
            _progress(
                progress,
                "extracting",
                len(snapshots),
                processed,
                searchable,
                len(snapshots) - processed,
            )
        removed_ids = self._missing_document_ids(rows, present, target)
        try:
            with self.db.transaction() as connection:
                for document_id, snapshot, metadata in stat_updates:
                    connection.execute(
                        "UPDATE documents SET size=?,modified_ns=?,metadata_json=? WHERE id=?",
                        (snapshot.size, snapshot.modified_ns, json.dumps(metadata), document_id),
                    )
                    self.db._refresh_metadata_index(connection, document_id)
                for document in prepared:
                    self._store(connection, document)
                    report["indexed"] += 1
                for document_id in removed_ids:
                    _delete_document(connection, document_id, self.db)
                    report["removed"] += 1
        except Exception as exc:
            report["errors"].append(f"index transaction: {exc}")
            return report
        searchable = len(snapshots) - len(report["errors"])
        _progress(progress, "committed", len(snapshots), processed, searchable, 0)
        embedding = self.embed_pending(progress)
        report["embedded"] = embedding["embedded"]
        report["warnings"].extend(embedding["warnings"])
        if report["indexed"] and self.embeddings is None:
            report["warnings"].append(
                "embeddings are not configured; FTS indexing succeeded but vectors are unavailable"
            )
        missing_ocr = sum(
            review.get("reason") == "ocr_runtime_missing"
            for document in prepared
            for review in document.extracted.reviews
        )
        if missing_ocr:
            report["warnings"].append(
                f"{missing_ocr} PDF page(s) require OCR and were added to the review queue"
            )
        return report

    def _classify(
        self,
        snapshot: FileSnapshot,
        current: Any,
        force_index: bool = False,
        reextract: bool = False,
    ) -> Any:
        if (
            current is not None
            and not force_index
            and not reextract
            and current["size"] == snapshot.size
            and current["modified_ns"] == snapshot.modified_ns
        ):
            return "unchanged"
        content_hash = _file_hash(snapshot.path)
        if current is not None and current["content_hash"] == content_hash and not reextract:
            if force_index:
                return self._prepare_cached_reindex(snapshot, current)
            metadata = json.loads(current["metadata_json"])
            metadata["size"] = snapshot.size
            metadata["modified_ns"] = snapshot.modified_ns
            return int(current["id"]), snapshot, metadata
        extracted, artifact = self._extract_cached(
            snapshot.path, content_hash, use_cache=not reextract
        )
        chunks = _chunks(extracted, self.settings.chunk_chars, self.settings.chunk_overlap)
        automatic = _automatic_metadata(snapshot, extracted, content_hash)
        return PreparedDocument(snapshot, content_hash, extracted, artifact, chunks, automatic)

    def _prepare_cached_reindex(self, snapshot: FileSnapshot, current: Any) -> PreparedDocument:
        effective = current["effective_artifact_path"]
        artifact = Path(effective or current["artifact_path"])
        if not artifact.is_file():
            kind = "effective corrected" if effective else "extracted"
            raise FileNotFoundError(
                f"cached {kind} artifact is missing: {artifact}; rerun with --reextract"
            )
        extracted = _document_from_artifact(json.loads(artifact.read_text(encoding="utf-8")))
        chunks = _chunks(extracted, self.settings.chunk_chars, self.settings.chunk_overlap)
        automatic = json.loads(current["metadata_json"])
        if current["source_url"]:
            automatic.update(
                {
                    "size": snapshot.size,
                    "modified_ns": snapshot.modified_ns,
                    "content_hash": current["content_hash"],
                    "word_count": len(extracted.text.split()),
                    "source_positions": len(extracted.spans),
                }
            )
        else:
            automatic.update(_automatic_metadata(snapshot, extracted, current["content_hash"]))
        return PreparedDocument(
            snapshot,
            current["content_hash"],
            extracted,
            Path(current["artifact_path"]),
            chunks,
            automatic,
            preserve_derived_state=True,
            relative_path=current["relative_path"],
            external_id=current["external_id"],
            source_version=current["source_version"],
            source_url=current["source_url"],
        )

    def index_file(
        self,
        path: Path,
        force_index: bool = False,
        reextract: bool = False,
        embed: bool = True,
    ) -> bool:
        snapshot = self.snapshot(path)
        if snapshot is None:
            return False
        current = self.db.document(snapshot.path, self.source_id)
        outcome = self._classify(snapshot, current, force_index, reextract)
        if outcome == "unchanged":
            return False
        with self.db.transaction() as connection:
            if isinstance(outcome, tuple):
                document_id, refreshed, metadata = outcome
                connection.execute(
                    "UPDATE documents SET size=?,modified_ns=?,metadata_json=? WHERE id=?",
                    (refreshed.size, refreshed.modified_ns, json.dumps(metadata), document_id),
                )
                self.db._refresh_metadata_index(connection, document_id)
                return False
            self._store(connection, outcome)
        if embed:
            self.embed_pending()
        return True

    def index_paths(self, paths: Sequence[Path]) -> dict[str, Any]:
        changed = 0
        errors = []
        for path in dict.fromkeys(path.resolve() for path in paths):
            try:
                changed += int(self.index_file(path, embed=False))
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        embedding = self.embed_pending()
        return {"changed": changed, "errors": errors, **embedding}

    def _store(self, connection: Any, document: PreparedDocument) -> int:
        snapshot = document.snapshot
        relative = (
            document.relative_path or snapshot.path.relative_to(self.settings.root).as_posix()
        )
        external_id = document.external_id or relative
        title = str(document.automatic["title"])
        existing = connection.execute(
            "SELECT id FROM documents WHERE source_id=? AND path=?",
            (self.source_id, str(snapshot.path)),
        ).fetchone()
        if existing:
            document_id = int(existing["id"])
            _delete_chunks(connection, document_id)
            if document.preserve_derived_state:
                connection.execute(
                    """UPDATE documents SET relative_path=?,external_id=?,source_version=?,
                       source_url=?,
                       size=?,modified_ns=?,title=?,
                       metadata_json=?,indexed_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        relative,
                        external_id,
                        document.source_version,
                        document.source_url,
                        snapshot.size,
                        snapshot.modified_ns,
                        title,
                        json.dumps(document.automatic),
                        document_id,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE documents SET relative_path=?,external_id=?,source_version=?,
                       source_url=?,
                       content_hash=?,size=?,modified_ns=?,
                       media_type=?,title=?,metadata_json=?,artifact_path=?,
                       effective_artifact_path=NULL,indexed_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        relative,
                        external_id,
                        document.source_version,
                        document.source_url,
                        document.content_hash,
                        snapshot.size,
                        snapshot.modified_ns,
                        document.extracted.media_type,
                        title,
                        json.dumps(document.automatic),
                        str(document.artifact_path),
                        document_id,
                    ),
                )
                connection.execute("DELETE FROM reviews WHERE document_id=?", (document_id,))
        else:
            cursor = connection.execute(
                """INSERT INTO documents
                   (path,relative_path,content_hash,size,modified_ns,media_type,title,
                    metadata_json,artifact_path,source_id,external_id,source_version,source_url)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(snapshot.path),
                    relative,
                    document.content_hash,
                    snapshot.size,
                    snapshot.modified_ns,
                    document.extracted.media_type,
                    title,
                    json.dumps(document.automatic),
                    str(document.artifact_path),
                    self.source_id,
                    external_id,
                    document.source_version,
                    document.source_url,
                ),
            )
            document_id = int(cursor.lastrowid)
        self._insert_chunks(connection, document_id, relative, title, document.chunks)
        for review in () if document.preserve_derived_state else document.extracted.reviews:
            connection.execute(
                """INSERT OR IGNORE INTO reviews
                   (document_id,path,content_hash,page,reason,detail_json) VALUES(?,?,?,?,?,?)""",
                (
                    document_id,
                    str(snapshot.path),
                    document.content_hash,
                    review.get("page"),
                    review["reason"],
                    json.dumps(review.get("detail", {})),
                ),
            )
        self.db._refresh_metadata_index(connection, document_id)
        return document_id

    def store_external(
        self,
        key: str,
        relative_path: str,
        external_id: str,
        content_hash: str,
        extracted: ExtractedDocument,
        artifact_path: Path,
        metadata: dict[str, Any],
        source_version: str = "",
        source_url: str = "",
    ) -> int:
        path = Path(key)
        prepared = PreparedDocument(
            FileSnapshot(path, int(metadata.get("size", 0)), int(metadata.get("modified_ns", 0))),
            content_hash,
            extracted,
            artifact_path,
            _chunks(extracted, self.settings.chunk_chars, self.settings.chunk_overlap),
            metadata,
            relative_path=relative_path,
            external_id=external_id,
            source_version=source_version,
            source_url=source_url,
        )
        with self.db.transaction() as connection:
            return self._store(connection, prepared)

    @staticmethod
    def _insert_chunks(
        connection: Any,
        document_id: int,
        relative: str,
        title: str,
        chunks: Sequence[ChunkRecord],
    ) -> None:
        for chunk in chunks:
            cursor = connection.execute(
                """INSERT INTO chunks
                   (document_id,ordinal,text,start_char,end_char,chunk_hash,provenance_json,page_number)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    chunk.ordinal,
                    chunk.text,
                    chunk.start,
                    chunk.end,
                    chunk.chunk_hash,
                    json.dumps(chunk.provenance),
                    _chunk_page(chunk.provenance),
                ),
            )
            connection.execute(
                "INSERT INTO chunks_fts(text,title,relative_path,chunk_id) VALUES(?,?,?,?)",
                (chunk.text, title, relative, int(cursor.lastrowid)),
            )

    def embed_pending(self, progress: ProgressCallback | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"embedded": 0, "warnings": []}
        if self.embeddings is None:
            return result
        provider, model = cache_identity(self.embeddings)
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT c.chunk_hash,min(c.text) text FROM chunks c
                   LEFT JOIN vectors v ON v.chunk_hash=c.chunk_hash
                     AND v.provider=? AND v.model=?
                   WHERE v.chunk_hash IS NULL GROUP BY c.chunk_hash ORDER BY c.chunk_hash""",
                (provider, model),
            ).fetchall()
        if progress is not None:
            progress({"phase": "embedding", "embedding_pending": len(rows)})
        for start in range(0, len(rows), self.embedding_batch_size):
            batch = rows[start : start + self.embedding_batch_size]
            try:
                vectors = self.embeddings.embed([row["text"] for row in batch])
                if len(vectors) != len(batch):
                    raise RuntimeError("provider returned an unexpected embedding count")
                with self.db.connect() as connection:
                    connection.executemany(
                        """INSERT OR IGNORE INTO vectors
                           (chunk_hash,provider,model,vector_json) VALUES(?,?,?,?)""",
                        [
                            (row["chunk_hash"], provider, model, json.dumps(vector))
                            for row, vector in zip(batch, vectors)  # noqa: B905
                        ],
                    )
                result["embedded"] += len(batch)
                if progress is not None:
                    progress(
                        {
                            "phase": "embedding",
                            "embedding_pending": max(0, len(rows) - result["embedded"]),
                        }
                    )
            except Exception as exc:
                result["warnings"].append(f"embeddings unavailable; FTS remains usable: {exc}")
                break
        return result

    def remove(self, path: Path) -> bool:
        row = self.db.document(path.resolve(), self.source_id)
        if row is None:
            return False
        with self.db.transaction() as connection:
            _delete_document(connection, int(row["id"]), self.db)
        return True

    def move(self, source: Path, destination: Path) -> bool:
        source, destination = source.resolve(), destination.resolve()
        source_row = self.db.document(source, self.source_id)
        snapshot = self.snapshot(destination)
        if snapshot is None:
            return self.remove(source)
        if source_row is None:
            return self.index_file(destination)
        unchanged = (
            source_row["size"] == snapshot.size
            and source_row["modified_ns"] == snapshot.modified_ns
        )
        if not unchanged and source_row["content_hash"] != _file_hash(destination):
            changed = self.index_file(destination)
            self.remove(source)
            return changed
        relative = destination.relative_to(self.settings.root).as_posix()
        metadata = json.loads(source_row["metadata_json"])
        metadata.update({"size": snapshot.size, "modified_ns": snapshot.modified_ns})
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE documents SET path=?,relative_path=?,external_id=?,size=?,
                   modified_ns=?,metadata_json=?
                   WHERE id=?""",
                (
                    str(destination),
                    relative,
                    relative,
                    snapshot.size,
                    snapshot.modified_ns,
                    json.dumps(metadata),
                    source_row["id"],
                ),
            )
            connection.execute(
                """UPDATE chunks_fts SET relative_path=?
                   WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)""",
                (relative, source_row["id"]),
            )
            connection.execute(
                "UPDATE reviews SET path=? WHERE document_id=?",
                (str(destination), source_row["id"]),
            )
            self.db._refresh_metadata_index(connection, int(source_row["id"]))
        return True

    def correct_review(
        self,
        review_id: int,
        corrected_text: str,
        evidence: Sequence[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        if not corrected_text.strip():
            raise ValueError("corrected page text must not be empty")
        self.db._validate_evidence(evidence)
        review = self.db.review(review_id)
        if review["page"] is None:
            raise ValueError("review item has no page number")
        base = json.loads(Path(review["artifact_path"]).read_text(encoding="utf-8"))
        with self.db.connect() as connection:
            previous = connection.execute(
                """SELECT page,corrected_text FROM review_revisions
                   WHERE document_id=? ORDER BY id""",
                (review["document_id"],),
            ).fetchall()
        corrections = {int(row["page"]): row["corrected_text"] for row in previous}
        corrections[int(review["page"])] = corrected_text.strip()
        effective = _apply_pdf_corrections(base, corrections, evidence, actor)
        revision_dir = self.settings.extracted_dir.parent / "revisions"
        revision_dir.mkdir(parents=True, exist_ok=True)
        revision_key = hashlib.sha256(
            f"{review['content_hash']}:{review_id}:{corrected_text}:{actor}".encode()
        ).hexdigest()
        artifact = revision_dir / f"{revision_key}.json"
        temporary = artifact.with_suffix(".tmp")
        temporary.write_text(json.dumps(effective, ensure_ascii=False), encoding="utf-8")
        temporary.replace(artifact)
        extracted = _document_from_artifact(effective)
        chunks = _chunks(extracted, self.settings.chunk_chars, self.settings.chunk_overlap)
        with self.db.connect() as connection:
            document_metadata = json.loads(
                connection.execute(
                    "SELECT metadata_json FROM documents WHERE id=?",
                    (review["document_id"],),
                ).fetchone()[0]
            )
        document_metadata.update(
            {
                "word_count": len(extracted.text.split()),
                "source_positions": len(extracted.spans),
                "review_corrections": len(corrections),
            }
        )
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO review_revisions
                   (review_id,document_id,page,corrected_text,evidence_json,actor,artifact_path)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    review_id,
                    review["document_id"],
                    review["page"],
                    corrected_text.strip(),
                    json.dumps(evidence),
                    actor,
                    str(artifact),
                ),
            )
            connection.execute(
                """UPDATE reviews SET status='resolved',resolution=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (f"corrected by {actor}", review_id),
            )
            connection.execute(
                """UPDATE documents SET effective_artifact_path=?,metadata_json=?
                   WHERE id=?""",
                (str(artifact), json.dumps(document_metadata), review["document_id"]),
            )
            _delete_chunks(connection, int(review["document_id"]))
            self._insert_chunks(
                connection,
                int(review["document_id"]),
                review["relative_path"],
                review["title"],
                chunks,
            )
            self.db._refresh_metadata_index(connection, int(review["document_id"]))
        embedding = self.embed_pending()
        return {
            "review_id": review_id,
            "status": "resolved",
            "page": review["page"],
            "artifact": str(artifact),
            **embedding,
        }

    def _extract_cached(
        self, path: Path, content_hash: str, use_cache: bool = True
    ) -> tuple[ExtractedDocument, Path]:
        artifact = self.settings.extracted_dir / f"{content_hash}.json"
        if use_cache and artifact.exists():
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            return _document_from_artifact(payload), artifact
        extracted = self.extractor.extract(path)
        payload = {
            "source_hash": content_hash,
            "text": extracted.text,
            "spans": [span.__dict__ for span in extracted.spans],
            "media_type": extracted.media_type,
            "metadata": extracted.metadata,
            "reviews": extracted.reviews,
        }
        artifact.parent.mkdir(parents=True, exist_ok=True)
        temporary = artifact.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(artifact)
        return extracted, artifact

    def _missing_document_ids(
        self, rows: Sequence[Any], present: set[str], target: Path | None
    ) -> list[int]:
        base = (target or self.settings.root).resolve()
        result = []
        for row in rows:
            path = Path(row["path"])
            in_scope = path == base if base.is_file() else path == base or base in path.parents
            if in_scope and str(path) not in present:
                result.append(int(row["id"]))
        return result


def _progress(
    callback: ProgressCallback | None,
    phase: str,
    discovered: int,
    processed: int,
    searchable: int,
    remaining: int,
) -> None:
    if callback is not None:
        callback(
            {
                "phase": phase,
                "discovered": discovered,
                "processed": processed,
                "searchable": searchable,
                "remaining": remaining,
            }
        )


def _delete_chunks(connection: Any, document_id: int) -> None:
    ids = [
        row[0]
        for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,))
    ]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        connection.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", ids)
    connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))


def _delete_document(connection: Any, document_id: int, database: Database | None = None) -> None:
    related = [
        row[0]
        for row in connection.execute(
            """SELECT CASE WHEN source_document_id=? THEN target_document_id
                       ELSE source_document_id END
               FROM relationships WHERE source_document_id=? OR target_document_id=?""",
            (document_id, document_id, document_id),
        )
    ]
    _delete_chunks(connection, document_id)
    connection.execute("DELETE FROM metadata_fts WHERE document_id=?", (document_id,))
    connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
    if database is not None:
        for related_id in set(related):
            database._refresh_metadata_index(connection, int(related_id))


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunks(document: ExtractedDocument, size: int, overlap: int) -> list[ChunkRecord]:
    chunks: list[ChunkRecord] = []
    start, length = 0, len(document.text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            boundary = max(
                document.text.rfind("\n\n", start + size // 2, end),
                document.text.rfind("\n", start + size // 2, end),
                document.text.rfind(" ", start + size // 2, end),
            )
            if boundary > start:
                end = boundary + 1
        raw = document.text[start:end]
        left = len(raw) - len(raw.lstrip())
        text = raw.strip()
        chunk_start, chunk_end = start + left, start + len(raw.rstrip())
        if text:
            provenance = [
                span.__dict__
                for span in document.spans
                if span.end > chunk_start and span.start < chunk_end
            ]
            chunks.append(
                ChunkRecord(
                    len(chunks),
                    text,
                    chunk_start,
                    chunk_end,
                    hashlib.sha256(text.encode()).hexdigest(),
                    provenance,
                )
            )
        if end >= length:
            break
        start = max(start + 1, end - overlap)
    return chunks


def _chunk_page(provenance: Sequence[dict[str, Any]]) -> int | None:
    pages = {
        int(item["locator"].split(":", 1)[1])
        for item in provenance
        if item.get("kind") == "pdf_page" and str(item.get("locator", "")).startswith("page:")
    }
    return next(iter(pages)) if len(pages) == 1 else None


def _automatic_metadata(
    snapshot: FileSnapshot, document: ExtractedDocument, content_hash: str
) -> dict[str, Any]:
    path = snapshot.path
    first = next((line.strip("# ") for line in document.text.splitlines() if line.strip()), "")
    title = first[:160] if path.suffix.lower() in {".md", ".markdown"} and first else path.stem
    return {
        "title": title,
        "extension": path.suffix.lower(),
        "size": snapshot.size,
        "modified_ns": snapshot.modified_ns,
        "content_hash": content_hash,
        "word_count": len(document.text.split()),
        "source_positions": len(document.spans),
        **document.metadata,
    }


def _document_from_artifact(payload: dict[str, Any]) -> ExtractedDocument:
    return ExtractedDocument(
        payload["text"],
        [SourceSpan(**span) for span in payload["spans"]],
        payload["media_type"],
        payload.get("metadata", {}),
        payload.get("reviews", []),
    )


def _apply_pdf_corrections(
    base: dict[str, Any],
    corrections: dict[int, str],
    evidence: Sequence[dict[str, Any]],
    actor: str,
) -> dict[str, Any]:
    pages: dict[int, tuple[str, dict[str, Any]]] = {}
    for span in base["spans"]:
        if span["kind"] != "pdf_page" or not span["locator"].startswith("page:"):
            continue
        page = int(span["locator"].split(":", 1)[1])
        pages[page] = (base["text"][span["start"] : span["end"]], span.get("metadata", {}))
    for page, text in corrections.items():
        pages[page] = (
            text.strip(),
            {"source": "review_correction", "actor": actor, "evidence": list(evidence)},
        )
    parts: list[str] = []
    spans = []
    length = 0
    for page, (text, metadata) in sorted(pages.items()):
        if not text.strip():
            continue
        separator = "\n\n" if parts else ""
        parts.append(separator + text.strip())
        start = length + len(separator)
        length = start + len(text.strip())
        spans.append(SourceSpan("pdf_page", f"page:{page}", start, length, metadata).__dict__)
    return {
        **base,
        "text": "".join(parts),
        "spans": spans,
        "reviews": [],
        "corrections": {str(page): text for page, text in corrections.items()},
    }
