from __future__ import annotations

import json
import os
import re
import sqlite3
import struct
import unicodedata
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

MIGRATIONS = [
    """
    CREATE TABLE documents (
      id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, relative_path TEXT NOT NULL,
      content_hash TEXT NOT NULL, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
      media_type TEXT NOT NULL, title TEXT NOT NULL, metadata_json TEXT NOT NULL,
      artifact_path TEXT NOT NULL, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE chunks (
      id INTEGER PRIMARY KEY,
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL, text TEXT NOT NULL,
      start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
      chunk_hash TEXT NOT NULL, provenance_json TEXT NOT NULL, UNIQUE(document_id, ordinal)
    );
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
      text, title, relative_path UNINDEXED, chunk_id UNINDEXED, tokenize='unicode61'
    );
    CREATE INDEX chunks_document_idx ON chunks(document_id);
    CREATE INDEX chunks_hash_idx ON chunks(chunk_hash);
    CREATE TABLE vectors (
      chunk_hash TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
      vector_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(chunk_hash, provider, model)
    );
    CREATE TABLE reviews (
      id INTEGER PRIMARY KEY, document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
      path TEXT NOT NULL, content_hash TEXT NOT NULL, page INTEGER, reason TEXT NOT NULL,
      detail_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', resolution TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(path, content_hash, page, reason)
    );
    CREATE TABLE agent_metadata (
      id INTEGER PRIMARY KEY,
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      key TEXT NOT NULL, value_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
      actor TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE relationships (
      id INTEGER PRIMARY KEY,
      source_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      target_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      relation TEXT NOT NULL, evidence_json TEXT NOT NULL, actor TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(source_document_id, target_document_id, relation)
    );
    """,
    """
    DROP TABLE IF EXISTS jobs;
    ALTER TABLE documents ADD COLUMN effective_artifact_path TEXT;
    CREATE TABLE review_revisions (
      id INTEGER PRIMARY KEY,
      review_id INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      page INTEGER NOT NULL, corrected_text TEXT NOT NULL, evidence_json TEXT NOT NULL,
      actor TEXT NOT NULL, artifact_path TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX review_revisions_document_idx ON review_revisions(document_id);
    CREATE INDEX agent_metadata_document_idx ON agent_metadata(document_id);
    CREATE INDEX relationships_source_idx ON relationships(source_document_id);
    CREATE INDEX relationships_target_idx ON relationships(target_document_id);
    CREATE VIRTUAL TABLE metadata_fts USING fts5(
      content, document_id UNINDEXED, kind UNINDEXED, provenance UNINDEXED,
      tokenize='unicode61'
    );
    INSERT INTO metadata_fts(content,document_id,kind,provenance)
      SELECT title || ' ' || metadata_json,id,'automatic','[]' FROM documents;
    """,
    """
    CREATE TABLE sources (
      id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('local','google_drive')),
      locator TEXT NOT NULL, account TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
      config_json TEXT NOT NULL DEFAULT '{}', cursor TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      last_sync_at TEXT, last_error TEXT
    );
    INSERT INTO sources(id,name,kind,locator,config_json)
      SELECT 'legacy','default','local','','{}'
      WHERE EXISTS(SELECT 1 FROM documents);
    ALTER TABLE documents ADD COLUMN source_id TEXT NOT NULL DEFAULT 'legacy';
    ALTER TABLE documents ADD COLUMN external_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE documents ADD COLUMN source_version TEXT NOT NULL DEFAULT '';
    ALTER TABLE documents ADD COLUMN source_url TEXT NOT NULL DEFAULT '';
    UPDATE documents SET external_id=path WHERE external_id='';
    CREATE UNIQUE INDEX documents_source_external_idx ON documents(source_id,external_id);
    CREATE INDEX documents_source_path_idx ON documents(source_id,relative_path);
    ALTER TABLE chunks ADD COLUMN page_number INTEGER;
    CREATE TABLE source_sync_state (
      source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      key TEXT NOT NULL, value_json TEXT NOT NULL,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(source_id,key)
    );
    """,
    """
    CREATE TABLE index_jobs (
      id TEXT PRIMARY KEY, kind TEXT NOT NULL,
      source TEXT, target TEXT, reextract INTEGER NOT NULL DEFAULT 0,
      full INTEGER NOT NULL DEFAULT 0,
      state TEXT NOT NULL CHECK(state IN ('queued','running','complete','error','rejected')),
      phase TEXT NOT NULL DEFAULT 'queued', discovered INTEGER NOT NULL DEFAULT 0,
      processed INTEGER NOT NULL DEFAULT 0, searchable INTEGER NOT NULL DEFAULT 0,
      remaining INTEGER NOT NULL DEFAULT 0, embedding_pending INTEGER NOT NULL DEFAULT 0,
      heartbeat_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      started_at TEXT, completed_at TEXT, error TEXT
    );
    CREATE INDEX index_jobs_state_created_idx ON index_jobs(state,created_at);
    CREATE TABLE index_writer_lease (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), job_id TEXT,
      heartbeat_at TEXT, FOREIGN KEY(job_id) REFERENCES index_jobs(id)
    );
    INSERT INTO index_writer_lease(singleton) VALUES(1);
    """,
    """
    DROP TABLE chunks_fts;
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
      text, title, relative_path UNINDEXED, chunk_id UNINDEXED,
      tokenize='unicode61 remove_diacritics 2'
    );
    INSERT INTO chunks_fts(text,title,relative_path,chunk_id)
      SELECT fts_fold(c.text),fts_fold(d.title),d.relative_path,c.id
      FROM chunks c JOIN documents d ON d.id=c.document_id;
    CREATE VIRTUAL TABLE metadata_fts_v3 USING fts5(
      content UNINDEXED, document_id UNINDEXED, kind UNINDEXED, provenance UNINDEXED,
      folded, tokenize='unicode61 remove_diacritics 2'
    );
    INSERT INTO metadata_fts_v3(content,document_id,kind,provenance,folded)
      SELECT content,document_id,kind,provenance,fts_fold(content) FROM metadata_fts;
    DROP TABLE metadata_fts;
    ALTER TABLE metadata_fts_v3 RENAME TO metadata_fts;
    """,
    """
    ALTER TABLE vectors ADD COLUMN vector_blob BLOB;
    UPDATE vectors SET vector_blob=vector_blob_from_json(vector_json);
    CREATE TRIGGER vector_blob_insert AFTER INSERT ON vectors BEGIN
      UPDATE vectors SET vector_blob=vector_blob_from_json(NEW.vector_json)
      WHERE chunk_hash=NEW.chunk_hash AND provider=NEW.provider AND model=NEW.model;
    END;
    CREATE TRIGGER vector_blob_update AFTER UPDATE OF vector_json ON vectors BEGIN
      UPDATE vectors SET vector_blob=vector_blob_from_json(NEW.vector_json)
      WHERE chunk_hash=NEW.chunk_hash AND provider=NEW.provider AND model=NEW.model;
    END;
    CREATE TABLE search_revision (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                                  revision INTEGER NOT NULL);
    INSERT INTO search_revision VALUES(1,0);
    """
    + "\n".join(
        f"CREATE TRIGGER search_revision_{table}_{operation} AFTER {operation} ON {table} "
        "BEGIN UPDATE search_revision SET revision=revision+1 WHERE singleton=1; END;"
        for table in ("vectors", "documents", "chunks", "sources")
        for operation in ("INSERT", "UPDATE", "DELETE")
    ),
    """
    CREATE TABLE document_id_sequence (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), next_id INTEGER NOT NULL
    );
    INSERT INTO document_id_sequence SELECT 1,COALESCE(MAX(id),0)+1 FROM documents;
    CREATE TRIGGER document_id_sequence_insert AFTER INSERT ON documents BEGIN
      UPDATE document_id_sequence SET next_id=MAX(next_id,NEW.id+1) WHERE singleton=1;
    END;
    """,
    """
    ALTER TABLE chunks ADD COLUMN cite_key TEXT;
    ALTER TABLE chunks ADD COLUMN section_path TEXT;
    ALTER TABLE chunks ADD COLUMN page_start INTEGER;
    ALTER TABLE chunks ADD COLUMN page_end INTEGER;
    ALTER TABLE chunks ADD COLUMN ocr INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX chunks_cite_key_idx ON chunks(document_id,cite_key);
    ALTER TABLE documents ADD COLUMN doc_no TEXT;
    ALTER TABLE documents ADD COLUMN doc_no_key TEXT;
    CREATE INDEX documents_doc_no_key_idx ON documents(doc_no_key);
    """,
    """
    CREATE TABLE document_validity (
      document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
      effective_from TEXT, effective_to TEXT,
      amended_by_json TEXT NOT NULL DEFAULT '[]', replaced_by TEXT,
      status_checked_on TEXT, status_source TEXT,
      evidence_json TEXT NOT NULL DEFAULT '[]', actor TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX document_validity_from_idx ON document_validity(effective_from);
    """,
    """
    CREATE TABLE daemon_heartbeat (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
      pid INTEGER NOT NULL, hostname TEXT NOT NULL,
      started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE documents ADD COLUMN trashed_at TEXT;
    CREATE INDEX documents_trashed_idx ON documents(trashed_at);
    """,
]


class DocumentTrashedError(ValueError):
    """The document exists but its Drive file is in the trash."""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30)
        if os.name != "nt":
            self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.create_function("fts_fold", 1, fts_fold, deterministic=True)
        connection.create_function(
            "vector_blob_from_json", 1, vector_blob_from_json, deterministic=True
        )
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # Another first-start process may be switching the new database to WAL. Its
            # migration transaction below still serializes this connection safely.
            if "locked" not in str(exc).lower():
                connection.close()
                raise
        return connection

    def migrate(self) -> None:
        connection = self.connect()
        try:
            # Serialize discovery and application. A concurrent first startup must not observe
            # the old schema and then race another process through the same DDL migration.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations
                   (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
            )
            applied = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, script in enumerate(MIGRATIONS, 1):
                if version not in applied:
                    _execute_script(connection, script)
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def document(self, path: Path, source_id: str | None = None) -> sqlite3.Row | None:
        with self.connect() as connection:
            if source_id is None:
                return cast(
                    sqlite3.Row | None,
                    connection.execute(
                        "SELECT * FROM documents WHERE path=?", (str(path),)
                    ).fetchone(),
                )
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM documents WHERE source_id=? AND path=?",
                    (source_id, str(path)),
                ).fetchone(),
            )

    def resolve_document(
        self, path: str, source: str | None = None, include_trashed: bool = False
    ) -> sqlite3.Row:
        row = self._resolve_document(path, source)
        if row["trashed_at"] and not include_trashed:
            raise DocumentTrashedError(
                "document_trashed: the Drive file is in the trash; it is hidden until restored"
            )
        return row

    def _resolve_document(self, path: str, source: str | None = None) -> sqlite3.Row:
        stable_sql = ""
        stable_values: list[Any] = []
        if path.startswith("drive:"):
            stable_sql = "s.kind='google_drive' AND d.external_id=?"
            stable_values = [path[6:]]
        elif path.startswith("local:") and len(path.split(":")) == 3:
            _, source_id, identifier = path.split(":")
            if identifier.isdigit():
                stable_sql = "s.kind='local' AND d.source_id=? AND d.id=?"
                stable_values = [source_id, int(identifier)]
        if stable_sql:
            if source:
                stable_sql += " AND (s.id=? OR s.name=?)"
                stable_values.extend((source, source))
            with self.connect() as connection:
                matches = connection.execute(
                    "SELECT d.*,s.name source_name,s.kind source_kind FROM documents d "
                    "JOIN sources s ON s.id=d.source_id WHERE " + stable_sql,
                    stable_values,
                ).fetchall()
            if len(matches) == 1:
                return cast(sqlite3.Row, matches[0])
            if len(matches) > 1:
                raise ValueError("stable reference is ambiguous; provide source")
        if source is None and ":" in path:
            possible_source, relative_path = path.split(":", 1)
            try:
                self.source(possible_source)
            except ValueError:
                pass
            else:
                source, path = possible_source, relative_path
        with self.connect() as connection:
            source_sql = ""
            values: list[Any] = [path, path, path]
            if source:
                source_sql = "AND (d.source_id=? OR s.name=?)"
                values.extend((source, source))
            rows = connection.execute(
                f"""SELECT d.*,s.name source_name,s.kind source_kind
                    FROM documents d LEFT JOIN sources s ON s.id=d.source_id
                    WHERE (d.path=? OR d.relative_path=? OR d.external_id=?) {source_sql}
                    ORDER BY d.id LIMIT 2""",
                values,
            ).fetchall()
        if not rows:
            raise ValueError(f"indexed document not found: {path}")
        if len(rows) > 1:
            raise ValueError(f"document reference is ambiguous; provide source: {path}")
        return cast(sqlite3.Row, rows[0])

    def document_snapshot(self, source_id: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if source_id is not None:
                return connection.execute(
                    "SELECT * FROM documents WHERE source_id=?", (source_id,)
                ).fetchall()
            return connection.execute("SELECT * FROM documents").fetchall()

    def add_source(
        self,
        name: str,
        kind: str,
        locator: str,
        account: str = "",
        config: dict[str, Any] | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"local", "google_drive"}:
            raise ValueError("source kind must be local or google_drive")
        if not name.strip() or any(character in name for character in "/\\:"):
            raise ValueError("source name must be non-empty and cannot contain /, \\, or :")
        identifier = source_id or uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO sources(id,name,kind,locator,account,config_json)
                   VALUES(?,?,?,?,?,?)""",
                (identifier, name.strip(), kind, locator, account, json.dumps(config or {})),
            )
        return self.source(identifier)

    def source(self, value: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                f"{_SOURCE_SELECT} WHERE s.id=? OR s.name=?", (value, value)
            ).fetchone()
        if row is None:
            raise ValueError(f"source not found: {value}")
        return _source_json(row)

    def sources(self, enabled: bool | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            sql, values = _SOURCE_SELECT, []
            if enabled is not None:
                sql += " WHERE s.enabled=?"
                values.append(int(enabled))
            rows = connection.execute(sql + " ORDER BY s.name", values).fetchall()
        return [_source_json(row) for row in rows]

    def set_source_enabled(self, value: str, enabled: bool) -> dict[str, Any]:
        source = self.source(value)
        with self.connect() as connection:
            connection.execute(
                """UPDATE sources SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (int(enabled), source["id"]),
            )
        return self.source(source["id"])

    def update_source_config(self, value: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Merge ``changes`` into one source's config_json (a None value removes the key)."""
        source = self.source(value)
        config = dict(source["config"])
        for key, item in changes.items():
            if item is None:
                config.pop(key, None)
            else:
                config[key] = item
        with self.connect() as connection:
            connection.execute(
                "UPDATE sources SET config_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(config), source["id"]),
            )
        return self.source(source["id"])

    def sync_state(self, source_id: str, key: str) -> Any | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM source_sync_state WHERE source_id=? AND key=?",
                (source_id, key),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def set_sync_state(self, source_id: str, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO source_sync_state(source_id,key,value_json) VALUES(?,?,?)
                   ON CONFLICT(source_id,key) DO UPDATE SET value_json=excluded.value_json,
                   updated_at=CURRENT_TIMESTAMP""",
                (source_id, key, json.dumps(value)),
            )

    def update_source_sync(
        self, source_id: str, cursor: str | None, error: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE sources SET cursor=?,last_sync_at=CURRENT_TIMESTAMP,last_error=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (cursor, error, source_id),
            )
            if error is None:
                connection.execute(
                    """INSERT INTO source_sync_state(source_id,key,value_json) VALUES(?,?,?)
                       ON CONFLICT(source_id,key) DO UPDATE SET value_json=excluded.value_json,
                       updated_at=CURRENT_TIMESTAMP""",
                    (
                        source_id,
                        SUCCESS_KEY,
                        json.dumps(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
                    ),
                )

    def purge_source(self, value: str) -> dict[str, Any]:
        source = self.source(value)
        with self.transaction() as connection:
            document_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM documents WHERE source_id=?", (source["id"],)
                )
            ]
            chunk_ids: list[int] = []
            related_document_ids: set[int] = set()
            if document_ids:
                placeholders = ",".join("?" for _ in document_ids)
                related_document_ids = {
                    int(row[0])
                    for row in connection.execute(
                        f"""SELECT target_document_id FROM relationships
                            WHERE source_document_id IN ({placeholders})
                            UNION
                            SELECT source_document_id FROM relationships
                            WHERE target_document_id IN ({placeholders})""",
                        [*document_ids, *document_ids],
                    )
                    if int(row[0]) not in document_ids
                }
                chunk_ids = [
                    int(row[0])
                    for row in connection.execute(
                        f"SELECT id FROM chunks WHERE document_id IN ({placeholders})", document_ids
                    )
                ]
                if chunk_ids:
                    chunk_placeholders = ",".join("?" for _ in chunk_ids)
                    connection.execute(
                        f"DELETE FROM chunks_fts WHERE chunk_id IN ({chunk_placeholders})",
                        chunk_ids,
                    )
                connection.execute(
                    f"DELETE FROM metadata_fts WHERE document_id IN ({placeholders})", document_ids
                )
                connection.execute(
                    f"DELETE FROM documents WHERE id IN ({placeholders})", document_ids
                )
                for document_id in related_document_ids:
                    self._refresh_metadata_index(connection, document_id)
            connection.execute("DELETE FROM sources WHERE id=?", (source["id"],))
            vectors_purged = int(
                connection.execute(
                    """SELECT count(*) FROM vectors v WHERE NOT EXISTS
                       (SELECT 1 FROM chunks c WHERE c.chunk_hash=v.chunk_hash)"""
                ).fetchone()[0]
            )
            connection.execute(
                """DELETE FROM vectors WHERE NOT EXISTS
                   (SELECT 1 FROM chunks c WHERE c.chunk_hash=vectors.chunk_hash)"""
            )
        return {
            "source": source["name"],
            "documents_purged": len(document_ids),
            "vectors_purged": vectors_purged,
        }

    def document_artifact_paths(
        self, document_ids: Sequence[int] = (), source_id: str | None = None
    ) -> set[str]:
        """Extraction/correction/revision artifact files used by the given documents or source."""
        values: list[Any]
        if source_id is not None:
            where, values = "d.source_id=?", [source_id]
        elif document_ids:
            where = f"d.id IN ({','.join('?' for _ in document_ids)})"
            values = list(document_ids)
        else:
            return set()
        with self.connect() as connection:
            paths = {
                str(path)
                for row in connection.execute(
                    f"SELECT artifact_path,effective_artifact_path FROM documents d WHERE {where}",
                    values,
                )
                for path in (row["artifact_path"], row["effective_artifact_path"])
                if path
            }
            paths.update(
                str(row["artifact_path"])
                for row in connection.execute(
                    f"""SELECT rr.artifact_path FROM review_revisions rr
                        JOIN documents d ON d.id=rr.document_id WHERE {where}""",
                    values,
                )
                if row["artifact_path"]
            )
        return paths

    def referenced_artifact_paths(self) -> set[str]:
        with self.connect() as connection:
            paths = {
                str(path)
                for row in connection.execute(
                    "SELECT artifact_path,effective_artifact_path FROM documents"
                )
                for path in (row["artifact_path"], row["effective_artifact_path"])
                if path
            }
            paths.update(
                str(row[0])
                for row in connection.execute("SELECT artifact_path FROM review_revisions")
                if row[0]
            )
        return paths

    def purge_orphan_vectors(self) -> int:
        with self.transaction() as connection:
            count = int(
                connection.execute(
                    """SELECT count(*) FROM vectors v WHERE NOT EXISTS
                       (SELECT 1 FROM chunks c WHERE c.chunk_hash=v.chunk_hash)"""
                ).fetchone()[0]
            )
            connection.execute(
                """DELETE FROM vectors WHERE NOT EXISTS
                   (SELECT 1 FROM chunks c WHERE c.chunk_hash=vectors.chunk_hash)"""
            )
        return count

    def stats(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                "documents": connection.execute("SELECT count(*) FROM documents").fetchone()[0],
                "chunks": connection.execute("SELECT count(*) FROM chunks").fetchone()[0],
                "vectors": connection.execute("SELECT count(*) FROM vectors").fetchone()[0],
                "open_reviews": connection.execute(
                    "SELECT count(*) FROM reviews WHERE status='open'"
                ).fetchone()[0],
                "review_revisions": connection.execute(
                    "SELECT count(*) FROM review_revisions"
                ).fetchone()[0],
                "sources": connection.execute("SELECT count(*) FROM sources").fetchone()[0],
                "enabled_sources": connection.execute(
                    "SELECT count(*) FROM sources WHERE enabled=1"
                ).fetchone()[0],
            }

    def list_reviews(self, status: str = "open") -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reviews WHERE status=? ORDER BY created_at,id", (status,)
            ).fetchall()
        return [_row_json(row, ("detail_json",)) for row in rows]

    def resolve_review(self, review_id: int, resolution: str) -> None:
        with self.connect() as connection:
            changed = connection.execute(
                """UPDATE reviews SET status='resolved',resolution=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (resolution, review_id),
            ).rowcount
        if not changed:
            raise ValueError(f"review not found: {review_id}")

    def add_metadata(
        self, path: str, key: str, value: Any, evidence: Sequence[dict[str, Any]], actor: str
    ) -> int:
        self._validate_evidence(evidence)
        document = self.resolve_document(path)
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO agent_metadata(document_id,key,value_json,evidence_json,actor)
                   VALUES(?,?,?,?,?)""",
                (document["id"], key, json.dumps(value), json.dumps(evidence), actor),
            )
            self._refresh_metadata_index(connection, int(document["id"]))
            if cursor.lastrowid is None:
                raise RuntimeError("metadata insert did not return a row id")
            return cursor.lastrowid

    def set_validity(
        self,
        path: str,
        fields: dict[str, Any],
        evidence: Sequence[dict[str, Any]],
        actor: str,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Record validity dates for one document (half-open: effective_to is exclusive)."""
        from .validity import normalize_validity

        self._validate_evidence(evidence)
        values = normalize_validity(fields)
        document = self.resolve_document(path, source)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO document_validity
                   (document_id,effective_from,effective_to,amended_by_json,replaced_by,
                    status_checked_on,status_source,evidence_json,actor)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                     effective_from=excluded.effective_from, effective_to=excluded.effective_to,
                     amended_by_json=excluded.amended_by_json, replaced_by=excluded.replaced_by,
                     status_checked_on=excluded.status_checked_on,
                     status_source=excluded.status_source, evidence_json=excluded.evidence_json,
                     actor=excluded.actor, updated_at=CURRENT_TIMESTAMP""",
                (
                    document["id"],
                    values["effective_from"],
                    values["effective_to"],
                    json.dumps(values["amended_by"], ensure_ascii=False),
                    values["replaced_by"],
                    values["status_checked_on"],
                    values["status_source"],
                    json.dumps(list(evidence)),
                    actor,
                ),
            )
        return {"stable_ref": stable_ref(document), **values}

    def metadata(self, path: str) -> dict[str, Any]:
        document = self.resolve_document(path)
        with self.connect() as connection:
            entries = connection.execute(
                "SELECT * FROM agent_metadata WHERE document_id=? ORDER BY id", (document["id"],)
            ).fetchall()
            relationships = connection.execute(
                """SELECT r.*, s.relative_path source_path, t.relative_path target_path
                   FROM relationships r JOIN documents s ON s.id=r.source_document_id
                   JOIN documents t ON t.id=r.target_document_id
                   WHERE r.source_document_id=? OR r.target_document_id=? ORDER BY r.id""",
                (document["id"], document["id"]),
            ).fetchall()
        return {
            "stable_ref": stable_ref(document),
            "automatic": json.loads(document["metadata_json"]),
            "agent": [_row_json(row, ("value_json", "evidence_json")) for row in entries],
            "relationships": [_row_json(row, ("evidence_json",)) for row in relationships],
        }

    def add_relationship(
        self,
        source: str,
        target: str,
        relation: str,
        evidence: Sequence[dict[str, Any]],
        actor: str,
    ) -> int:
        self._validate_evidence(evidence)
        source_doc, target_doc = self.resolve_document(source), self.resolve_document(target)
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO relationships
                   (source_document_id,target_document_id,relation,evidence_json,actor)
                   VALUES(?,?,?,?,?) ON CONFLICT(source_document_id,target_document_id,relation)
                   DO UPDATE SET evidence_json=excluded.evidence_json,actor=excluded.actor""",
                (source_doc["id"], target_doc["id"], relation, json.dumps(evidence), actor),
            )
            self._refresh_metadata_index(connection, int(source_doc["id"]))
            self._refresh_metadata_index(connection, int(target_doc["id"]))
            if cursor.lastrowid is None:
                raise RuntimeError("relationship insert did not return a row id")
            return cursor.lastrowid

    def review(self, review_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT r.*,d.relative_path,d.title,d.artifact_path,d.effective_artifact_path,
                          d.source_id
                   FROM reviews r JOIN documents d ON d.id=r.document_id WHERE r.id=?""",
                (review_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"review not found: {review_id}")
        return cast(sqlite3.Row, row)

    def _refresh_metadata_index(self, connection: sqlite3.Connection, document_id: int) -> None:
        connection.execute("DELETE FROM metadata_fts WHERE document_id=?", (document_id,))
        document = connection.execute(
            "SELECT title,metadata_json FROM documents WHERE id=?", (document_id,)
        ).fetchone()
        if document is None:
            return
        connection.execute(
            "INSERT INTO metadata_fts(content,document_id,kind,provenance) VALUES(?,?,?,?)",
            (f"{document['title']} {document['metadata_json']}", document_id, "automatic", "[]"),
        )
        for entry in connection.execute(
            "SELECT key,value_json,evidence_json FROM agent_metadata WHERE document_id=?",
            (document_id,),
        ):
            connection.execute(
                "INSERT INTO metadata_fts(content,document_id,kind,provenance) VALUES(?,?,?,?)",
                (
                    f"{entry['key']} {entry['value_json']}",
                    document_id,
                    "agent_metadata",
                    entry["evidence_json"],
                ),
            )
        for relationship in connection.execute(
            """SELECT r.relation,r.evidence_json,s.relative_path source,t.relative_path target
               FROM relationships r JOIN documents s ON s.id=r.source_document_id
               JOIN documents t ON t.id=r.target_document_id
               WHERE r.source_document_id=? OR r.target_document_id=?""",
            (document_id, document_id),
        ):
            connection.execute(
                "INSERT INTO metadata_fts(content,document_id,kind,provenance) VALUES(?,?,?,?)",
                (
                    f"{relationship['relation']} {relationship['source']} {relationship['target']}",
                    document_id,
                    "relationship",
                    relationship["evidence_json"],
                ),
            )
        connection.execute(
            "UPDATE metadata_fts SET folded=fts_fold(content) WHERE document_id=?", (document_id,)
        )

    def _validate_evidence(self, evidence: Sequence[dict[str, Any]]) -> None:
        if not evidence:
            raise ValueError("evidence is required")
        for item in evidence:
            if not isinstance(item, dict) or not item.get("path"):
                raise ValueError("each evidence item requires an indexed path")
            if not item.get("locator") and not item.get("quote"):
                raise ValueError("each evidence item requires a locator or quote")
            reference = str(item["path"])
            try:
                self.resolve_document(reference)
            except ValueError:
                if ":" not in reference:
                    raise
                source, path = reference.split(":", 1)
                self.resolve_document(path, source)


SUCCESS_KEY = "last_success_at"
_SOURCE_SELECT = (
    "SELECT s.*, (SELECT value_json FROM source_sync_state "
    f"WHERE source_id=s.id AND key='{SUCCESS_KEY}') AS last_success_json FROM sources s"
)


def _source_json(row: sqlite3.Row) -> dict[str, Any]:
    result = _row_json(row, ("config_json",))
    raw = result.pop("last_success_json", None)
    result["last_success_at"] = json.loads(raw) if raw else None
    return result


def _row_json(row: sqlite3.Row, json_fields: Sequence[str]) -> dict[str, Any]:
    result = dict(row)
    for field in json_fields:
        result[field[:-5] if field.endswith("_json") else field] = json.loads(result.pop(field))
    return result


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute migration statements without sqlite3.executescript's implicit commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete SQL migration statement")


def stable_ref(document: Any) -> str:
    if document["source_kind"] == "google_drive":
        return f"drive:{document['external_id']}"
    keys = set(document.keys())
    identifier = document["document_id"] if "document_id" in keys else document["id"]
    return f"local:{document['source_id']}:{identifier}"


def fts_fold(text: str) -> str:
    # Metadata JSON may contain escaped Unicode. Original display text stays intact.
    text = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: match[0] if 0xD800 <= int(match[1], 16) <= 0xDFFF else chr(int(match[1], 16)),
        text,
    )
    return unicodedata.normalize("NFC", text).replace("đ", "d").replace("Đ", "D")


def vector_blob_from_json(value: str) -> bytes | None:
    try:
        vector = json.loads(value)
        return struct.pack(f"<{len(vector)}f", *vector)
    except (ValueError, TypeError, OverflowError, struct.error):
        # Preserve historical JSON even if it cannot be represented as float32.
        return None
