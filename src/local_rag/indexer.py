import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .config import Settings
from .db import Database
from .embeddings import EmbeddingProvider
from .extract import Extractor
from .models import ChunkRecord, ExtractedDocument, SourceSpan


class Indexer:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        extractor: Extractor,
        embeddings: Optional[EmbeddingProvider] = None,
    ):
        self.settings, self.db, self.extractor, self.embeddings = (
            settings,
            database,
            extractor,
            embeddings,
        )

    def files(self, target: Optional[Path] = None) -> Iterator[Path]:
        base = (target or self.settings.root).resolve()
        if not self.settings.contains(base) or self.settings.excluded(base):
            raise ValueError("target is outside the configured root or excluded")
        if base.is_file():
            if self.settings.accepts(base):
                yield base
            return
        for directory, names, files in os.walk(base, followlinks=False):
            parent = Path(directory)
            names[:] = [
                name
                for name in names
                if not self.settings.excluded(parent / name) and not (parent / name).is_symlink()
            ]
            for name in sorted(files):
                candidate = (parent / name).resolve()
                if self.settings.accepts(candidate) and candidate.is_file():
                    yield candidate

    def reconcile(self, target: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
        job_id = self.db.start_job(
            "reconcile" if not force else "reindex", str(target or self.settings.root)
        )
        report: Dict[str, Any] = {
            "job_id": job_id,
            "discovered": 0,
            "indexed": 0,
            "unchanged": 0,
            "removed": 0,
            "errors": [],
        }
        present: Set[str] = set()
        try:
            for path in self.files(target):
                report["discovered"] += 1
                present.add(str(path))
                try:
                    changed = self.index_file(path, force=force)
                    report["indexed" if changed else "unchanged"] += 1
                except Exception as exc:
                    report["errors"].append(f"{path}: {exc}")
            report["removed"] = self.remove_missing(present, target)
            self.db.finish_job(job_id, "failed" if report["errors"] else "completed", report)
            return report
        except Exception as exc:
            report["errors"].append(str(exc))
            self.db.finish_job(job_id, "failed", report)
            raise

    def index_file(self, path: Path, force: bool = False) -> bool:
        path = path.resolve()
        if not self.settings.accepts(path) or not path.is_file():
            return False
        content_hash = _file_hash(path)
        current = self.db.document(path)
        if current is not None and current["content_hash"] == content_hash and not force:
            self._ensure_vectors_for_document(int(current["id"]))
            return False
        extracted, artifact_path = self._extract_cached(path, content_hash, use_cache=not force)
        chunks = _chunks(extracted, self.settings.chunk_chars, self.settings.chunk_overlap)
        vectors = self._vectors(chunks)
        stat = path.stat()
        relative = path.relative_to(self.settings.root).as_posix()
        automatic = _automatic_metadata(
            path, extracted, content_hash, stat.st_size, stat.st_mtime_ns
        )
        title = str(automatic["title"])
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM documents WHERE path=?", (str(path),)
            ).fetchone()
            if existing:
                document_id = int(existing["id"])
                _delete_chunks(connection, document_id)
                connection.execute(
                    """UPDATE documents SET relative_path=?,content_hash=?,size=?,modified_ns=?,
                       media_type=?,title=?,metadata_json=?,artifact_path=?,indexed_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        relative,
                        content_hash,
                        stat.st_size,
                        stat.st_mtime_ns,
                        extracted.media_type,
                        title,
                        json.dumps(automatic),
                        str(artifact_path),
                        document_id,
                    ),
                )
                connection.execute("DELETE FROM reviews WHERE document_id=?", (document_id,))
            else:
                cursor = connection.execute(
                    """INSERT INTO documents
                       (path,relative_path,content_hash,size,modified_ns,media_type,title,metadata_json,artifact_path)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        str(path),
                        relative,
                        content_hash,
                        stat.st_size,
                        stat.st_mtime_ns,
                        extracted.media_type,
                        title,
                        json.dumps(automatic),
                        str(artifact_path),
                    ),
                )
                document_id = int(cursor.lastrowid)
            for chunk in chunks:
                cursor = connection.execute(
                    """INSERT INTO chunks
                       (document_id,ordinal,text,start_char,end_char,chunk_hash,provenance_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        document_id,
                        chunk.ordinal,
                        chunk.text,
                        chunk.start,
                        chunk.end,
                        chunk.chunk_hash,
                        json.dumps(chunk.provenance),
                    ),
                )
                connection.execute(
                    "INSERT INTO chunks_fts(text,title,relative_path,chunk_id) VALUES(?,?,?,?)",
                    (chunk.text, title, relative, int(cursor.lastrowid)),
                )
            for chunk_hash, provider, model, vector in vectors:
                connection.execute(
                    """INSERT OR IGNORE INTO vectors(chunk_hash,provider,model,vector_json)
                       VALUES(?,?,?,?)""",
                    (chunk_hash, provider, model, json.dumps(vector)),
                )
            for review in extracted.reviews:
                connection.execute(
                    """INSERT OR IGNORE INTO reviews
                       (document_id,path,content_hash,page,reason,detail_json) VALUES(?,?,?,?,?,?)""",
                    (
                        document_id,
                        str(path),
                        content_hash,
                        review.get("page"),
                        review["reason"],
                        json.dumps(review.get("detail", {})),
                    ),
                )
        return True

    def remove(self, path: Path) -> bool:
        path = path.resolve()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM documents WHERE path=?", (str(path),)
            ).fetchone()
            if not row:
                return False
            _delete_chunks(connection, int(row["id"]))
            connection.execute("DELETE FROM documents WHERE id=?", (row["id"],))
            return True

    def move(self, source: Path, destination: Path) -> bool:
        source, destination = source.resolve(), destination.resolve()
        if not destination.exists() or not self.settings.accepts(destination):
            return self.remove(source)
        source_row = self.db.document(source)
        if source_row is None or source_row["content_hash"] != _file_hash(destination):
            changed = self.index_file(destination)
            self.remove(source)
            return changed
        relative = destination.relative_to(self.settings.root).as_posix()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE documents SET path=?,relative_path=? WHERE id=?",
                (str(destination), relative, source_row["id"]),
            )
            connection.execute(
                "UPDATE chunks_fts SET relative_path=? WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)",
                (relative, source_row["id"]),
            )
            connection.execute(
                "UPDATE reviews SET path=? WHERE document_id=?",
                (str(destination), source_row["id"]),
            )
        return True

    def remove_missing(self, present: Set[str], target: Optional[Path]) -> int:
        base = (target or self.settings.root).resolve()
        removed = 0
        with self.db.connect() as connection:
            paths = [Path(row[0]) for row in connection.execute("SELECT path FROM documents")]
        for path in paths:
            in_scope = path == base if base.is_file() else (path == base or base in path.parents)
            if in_scope and str(path) not in present:
                removed += int(self.remove(path))
        return removed

    def _extract_cached(
        self, path: Path, content_hash: str, use_cache: bool = True
    ) -> Tuple[ExtractedDocument, Path]:
        artifact = self.settings.extracted_dir / f"{content_hash}.json"
        if use_cache and artifact.exists():
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            spans = [SourceSpan(**span) for span in payload["spans"]]
            return ExtractedDocument(
                payload["text"],
                spans,
                payload["media_type"],
                payload["metadata"],
                payload["reviews"],
            ), artifact
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

    def _vectors(
        self, chunks: Sequence[ChunkRecord]
    ) -> List[Tuple[str, str, str, Sequence[float]]]:
        if self.embeddings is None or not chunks:
            return []
        provider, model = self.embeddings.identity
        missing: List[ChunkRecord] = []
        with self.db.connect() as connection:
            for chunk in chunks:
                row = connection.execute(
                    "SELECT 1 FROM vectors WHERE chunk_hash=? AND provider=? AND model=?",
                    (chunk.chunk_hash, provider, model),
                ).fetchone()
                if row is None:
                    missing.append(chunk)
        if not missing:
            return []
        embedded = self.embeddings.embed([chunk.text for chunk in missing])
        return [
            (chunk.chunk_hash, provider, model, vector) for chunk, vector in zip(missing, embedded)
        ]

    def _ensure_vectors_for_document(self, document_id: int) -> None:
        if self.embeddings is None:
            return
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chunks WHERE document_id=? ORDER BY ordinal", (document_id,)
            ).fetchall()
        chunks = [
            ChunkRecord(
                row["ordinal"],
                row["text"],
                row["start_char"],
                row["end_char"],
                row["chunk_hash"],
                json.loads(row["provenance_json"]),
            )
            for row in rows
        ]
        vectors = self._vectors(chunks)
        if vectors:
            with self.db.connect() as connection:
                connection.executemany(
                    "INSERT OR IGNORE INTO vectors(chunk_hash,provider,model,vector_json) VALUES(?,?,?,?)",
                    [(h, p, m, json.dumps(v)) for h, p, m, v in vectors],
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


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunks(document: ExtractedDocument, size: int, overlap: int) -> List[ChunkRecord]:
    chunks: List[ChunkRecord] = []
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
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            chunks.append(
                ChunkRecord(len(chunks), text, chunk_start, chunk_end, digest, provenance)
            )
        if end >= length:
            break
        start = max(start + 1, end - overlap)
    return chunks


def _automatic_metadata(
    path: Path, document: ExtractedDocument, content_hash: str, size: int, modified_ns: int
) -> Dict[str, Any]:
    first = next((line.strip("# ") for line in document.text.splitlines() if line.strip()), "")
    title = first[:160] if path.suffix.lower() in {".md", ".markdown"} and first else path.stem
    return {
        "title": title,
        "extension": path.suffix.lower(),
        "size": size,
        "modified_ns": modified_ns,
        "content_hash": content_hash,
        "word_count": len(document.text.split()),
        "source_positions": len(document.spans),
        **document.metadata,
    }
