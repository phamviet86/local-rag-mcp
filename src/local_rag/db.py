from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
]


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30)
        if os.name != "nt":
            self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations
                   (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
            )
            applied = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, script in enumerate(MIGRATIONS, 1):
                if version not in applied:
                    connection.executescript(script)
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )

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
                return connection.execute(
                    "SELECT * FROM documents WHERE path=?", (str(path),)
                ).fetchone()
            return connection.execute(
                "SELECT * FROM documents WHERE source_id=? AND path=?",
                (source_id, str(path)),
            ).fetchone()

    def resolve_document(self, path: str, source: str | None = None) -> sqlite3.Row:
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
        return rows[0]

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
                "SELECT * FROM sources WHERE id=? OR name=?", (value, value)
            ).fetchone()
        if row is None:
            raise ValueError(f"source not found: {value}")
        return _row_json(row, ("config_json",))

    def sources(self, enabled: bool | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            sql, values = "SELECT * FROM sources", []
            if enabled is not None:
                sql += " WHERE enabled=?"
                values.append(int(enabled))
            rows = connection.execute(sql + " ORDER BY name", values).fetchall()
        return [_row_json(row, ("config_json",)) for row in rows]

    def set_source_enabled(self, value: str, enabled: bool) -> dict[str, Any]:
        source = self.source(value)
        with self.connect() as connection:
            connection.execute(
                """UPDATE sources SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (int(enabled), source["id"]),
            )
        return self.source(source["id"])

    def update_source_sync(
        self, source_id: str, cursor: str | None, error: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE sources SET cursor=?,last_sync_at=CURRENT_TIMESTAMP,last_error=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (cursor, error, source_id),
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
            return int(cursor.lastrowid)

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
            return int(cursor.lastrowid)

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
        return row

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


def _row_json(row: sqlite3.Row, json_fields: Sequence[str]) -> dict[str, Any]:
    result = dict(row)
    for field in json_fields:
        result[field[:-5] if field.endswith("_json") else field] = json.loads(result.pop(field))
    return result
