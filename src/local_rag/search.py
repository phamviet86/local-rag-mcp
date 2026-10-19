from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, fts_fold, stable_ref
from .embeddings import EmbeddingProvider, cache_identity

SEARCH_MODES = ("full_text", "semantic", "hybrid")


class SemanticSearchUnavailable(RuntimeError):
    pass


class SearchEngine:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        embeddings: EmbeddingProvider | None = None,
        query_cache_size: int = 128,
    ):
        self.settings = settings
        self.db = database
        self.embeddings = embeddings
        self.query_cache_size = query_cache_size
        self._query_cache: OrderedDict[tuple[str, str, str], Sequence[float]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._matrix_cache: OrderedDict[tuple[str, str], tuple[int, dict[str, int], Any, Any]] = (
            OrderedDict()
        )

    def search(
        self,
        query: str,
        limit: int = 8,
        scope: str | None = None,
        mode: str = "hybrid",
        source: str | None = None,
        per_document: int = 3,
        media_type: str | None = None,
        modified_after: str | None = None,
        modified_before: str | None = None,
    ) -> dict[str, Any]:
        if mode not in SEARCH_MODES:
            raise ValueError(f"mode must be one of: {', '.join(SEARCH_MODES)}")
        if not 1 <= per_document <= 100:
            raise ValueError("per_document must be between 1 and 100")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("query must contain searchable terms")
        relative_scope = self._relative_scope(scope)
        filters = (media_type, modified_after, modified_before)
        _filter_sql(relative_scope, source, *filters)  # Validate even for an empty index.
        warnings: list[str] = []
        lexical = (
            self._full_text(terms, relative_scope, source, limit, filters)
            if mode != "semantic"
            else {}
        )
        semantic: dict[int, tuple[int, float, Any]] = {}
        if mode != "full_text":
            try:
                semantic = self._semantic(query, relative_scope, source, limit, filters)
            except Exception as exc:
                if mode == "semantic":
                    raise SemanticSearchUnavailable(f"semantic search unavailable: {exc}") from exc
                warnings.append(f"semantic search unavailable; using full_text: {exc}")
        effective_mode = mode
        if mode == "hybrid" and not semantic:
            effective_mode = "full_text"
        results = self._combine(lexical, semantic, limit, effective_mode)
        results = self._diversify(results, per_document, limit, relative_scope, source, filters)
        return {
            "mode": mode,
            "effective_mode": effective_mode,
            "scope": relative_scope,
            "source": source,
            "warnings": warnings,
            "results": results,
        }

    def _full_text(
        self,
        terms: Sequence[str],
        scope: str | None,
        source: str | None,
        limit: int,
        filters: tuple[str | None, str | None, str | None] = (None, None, None),
    ) -> dict[tuple[str, int], dict[str, Any]]:
        fts_query = " OR ".join(f'"{fts_fold(term)}"' for term in terms)
        filter_sql, filter_values = _filter_sql(scope, source, *filters)
        candidate_limit = max(20, limit * 4)
        with self.db.connect() as connection:
            chunks = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                           d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,
                           s.name source_name,s.kind source_kind
                    FROM chunks_fts JOIN chunks c ON c.id=CAST(chunks_fts.chunk_id AS INTEGER)
                    JOIN documents d ON d.id=c.document_id
                    JOIN sources s ON s.id=d.source_id
                    WHERE chunks_fts MATCH ? AND s.enabled=1 {filter_sql}
                    ORDER BY bm25(chunks_fts) LIMIT ?""",
                [fts_query, *filter_values, candidate_limit],
            ).fetchall()
            metadata = connection.execute(
                f"""SELECT m.rowid metadata_id,m.content,m.kind,m.provenance,
                           d.id document_id,d.relative_path,d.title,d.content_hash,d.metadata_json,
                           d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,
                           s.name source_name,s.kind source_kind
                    FROM metadata_fts m JOIN documents d ON d.id=CAST(m.document_id AS INTEGER)
                    JOIN sources s ON s.id=d.source_id
                    WHERE metadata_fts MATCH ? AND s.enabled=1 {filter_sql}
                    ORDER BY bm25(metadata_fts) LIMIT ?""",
                [fts_query, *filter_values, candidate_limit],
            ).fetchall()
        # Prefer exact accent matches within the lexical candidate set, retaining
        # SQLite rank order among ties. The source text remains authoritative.
        chunks = sorted(
            chunks,
            key=lambda row: -sum(term.casefold() in row["text"].casefold() for term in terms),
        )
        results: dict[tuple[str, int], dict[str, Any]] = {}
        for rank, row in enumerate(chunks):
            results[("chunk", int(row["id"]))] = {
                "row": row,
                "lexical_rank": rank + 1,
                "semantic_rank": None,
                "semantic": 0.0,
                "matched_via": "content",
            }
        offset = len(chunks)
        for rank, row in enumerate(metadata):
            key = ("metadata", int(row["metadata_id"]))
            results[key] = {
                "row": row,
                "lexical_rank": offset + rank + 1,
                "semantic_rank": None,
                "semantic": 0.0,
                "matched_via": row["kind"],
            }
        return results

    def _semantic(
        self,
        query: str,
        scope: str | None,
        source: str | None,
        limit: int,
        filters: tuple[str | None, str | None, str | None] = (None, None, None),
    ) -> dict[int, tuple[int, float, Any]]:
        if self.embeddings is None:
            raise RuntimeError("no embedding provider is configured")
        provider, model = cache_identity(self.embeddings)
        query_vector = self._query_vector(query, provider, model)
        filter_sql, filter_values = _filter_sql(scope, source, *filters)
        with self.db.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                           d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,
                           s.name source_name,s.kind source_kind,
                           v.vector_json,v.vector_blob
                    FROM chunks c JOIN documents d ON d.id=c.document_id
                    JOIN sources s ON s.id=d.source_id
                    JOIN vectors v ON v.chunk_hash=c.chunk_hash
                    WHERE v.provider=? AND v.model=? AND s.enabled=1 {filter_sql}""",
                [provider, model, *filter_values],
            ).fetchall()
            scores = self._vector_scores(connection, query_vector, provider, model, rows)
        if not rows:
            raise RuntimeError("no cached vectors exist for the configured provider and model")
        scored = sorted(
            zip(scores, rows, strict=True),
            key=lambda item: item[0],
            reverse=True,
        )[: max(20, limit * 4)]
        return {
            int(row["id"]): (rank, max(0.0, (score + 1) / 2), row)
            for rank, (score, row) in enumerate(scored, 1)
        }

    def _vector_scores(
        self,
        connection: Any,
        query: Sequence[float],
        provider: str,
        model: str,
        rows: Sequence[Any],
    ) -> list[float]:
        np = _numpy()
        if np is None or not rows:
            return [_cosine(query, json.loads(row["vector_json"])) for row in rows]
        revision = int(
            connection.execute("SELECT revision FROM search_revision WHERE singleton=1").fetchone()[
                0
            ]
        )
        key = provider, model
        with self._cache_lock:
            cached = self._matrix_cache.get(key)
            if cached is None or cached[0] != revision:
                vectors = connection.execute(
                    "SELECT chunk_hash,vector_blob FROM vectors WHERE provider=? AND model=? "
                    "ORDER BY chunk_hash",
                    key,
                ).fetchall()
                if any(
                    row["vector_blob"] is None or len(row["vector_blob"]) != len(query) * 4
                    for row in vectors
                ):
                    return [_cosine(query, json.loads(row["vector_json"])) for row in rows]
                matrix = np.stack(
                    [np.frombuffer(row["vector_blob"], dtype="<f4") for row in vectors]
                )
                norms = np.linalg.norm(matrix, axis=1)
                cached = (
                    revision,
                    {row["chunk_hash"]: i for i, row in enumerate(vectors)},
                    matrix,
                    norms,
                )
                self._matrix_cache[key] = cached
                self._matrix_cache.move_to_end(key)
                while len(self._matrix_cache) > 4:
                    self._matrix_cache.popitem(last=False)
            _, indices, matrix, norms = cached
        vector = np.asarray(query, dtype=np.float32)
        if (
            matrix.shape[1] != len(query)
            or not np.all(np.isfinite(matrix))
            or not np.all(np.isfinite(vector))
        ):
            return [_cosine(query, json.loads(row["vector_json"])) for row in rows]
        denominator = norms * np.linalg.norm(vector)
        scores = np.divide(
            matrix @ vector, denominator, out=np.zeros_like(norms), where=denominator != 0
        )
        return [float(scores[indices[row["chunk_hash"]]]) for row in rows]

    def _query_vector(self, query: str, provider: str, model: str) -> Sequence[float]:
        key = provider, model, query
        with self._cache_lock:
            cached = self._query_cache.get(key)
            if cached is not None:
                self._query_cache.move_to_end(key)
                return cached
        embeddings = self.embeddings
        if embeddings is None:
            raise RuntimeError("no embedding provider is configured")
        vector = embeddings.embed([query])[0]
        with self._cache_lock:
            self._query_cache[key] = vector
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > self.query_cache_size:
                self._query_cache.popitem(last=False)
        return vector

    def _combine(
        self,
        lexical: dict[tuple[str, int], dict[str, Any]],
        semantic: dict[int, tuple[int, float, Any]],
        limit: int,
        mode: str,
    ) -> list[dict[str, Any]]:
        candidates = dict(lexical)
        for chunk_id, (rank, cosine_score, row) in semantic.items():
            key = ("chunk", chunk_id)
            entry = candidates.setdefault(
                key,
                {
                    "row": row,
                    "lexical_rank": None,
                    "semantic_rank": None,
                    "semantic": 0.0,
                    "matched_via": "semantic",
                },
            )
            entry["semantic_rank"] = rank
            entry["semantic"] = cosine_score
            if entry["matched_via"] == "content":
                entry["matched_via"] = "content+semantic"
        output = []
        for entry in candidates.values():
            lexical_rank = entry.get("lexical_rank")
            lexical_score = 1.0 / lexical_rank if lexical_rank else 0.0
            semantic_score = float(entry["semantic"])
            if mode == "semantic":
                score = semantic_score
            elif mode == "hybrid":
                score = _rrf(lexical_rank, entry.get("semantic_rank"))
            else:
                score = lexical_score
            output.append(self._result(entry, score))
        return sorted(output, key=lambda item: item["score"], reverse=True)

    def _diversify(
        self,
        results: list[dict[str, Any]],
        per_document: int,
        limit: int,
        scope: str | None,
        source: str | None,
        filters: tuple[str | None, str | None, str | None],
    ) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        seen: set[str] = set()
        selected = []
        for result in results:
            key = result["chunk_hash"]
            if key and key in seen:
                continue
            if counts[result["stable_ref"]] >= per_document:
                continue
            counts[result["stable_ref"]] += 1
            if key:
                seen.add(key)
            selected.append(result)
            if len(selected) == limit:
                break
        # Fetch every scoped location, including copies outside the ranked candidate
        # window. Never disclose disabled sources or locations outside strict scope.
        filter_sql, values = _filter_sql(scope, source, *filters)
        with self.db.connect() as connection:
            for result in selected:
                if not result["chunk_hash"]:
                    continue
                rows = connection.execute(
                    f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                        d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,
                        s.name source_name,s.kind source_kind FROM chunks c
                        JOIN documents d ON d.id=c.document_id JOIN sources s ON s.id=d.source_id
                        WHERE c.chunk_hash=? AND s.enabled=1 {filter_sql}
                        ORDER BY d.id,c.ordinal""",
                    [result["chunk_hash"], *values],
                ).fetchall()
                result["locations"] = [
                    {
                        key: hit[key]
                        for key in (
                            "stable_ref",
                            "document_ref",
                            "source",
                            "path",
                            "chunk_ordinal",
                            "start_char",
                            "end_char",
                            "citation",
                        )
                    }
                    for row in rows
                    for hit in [
                        self._result({"row": row, "matched_via": "content"}, result["score"])
                    ]
                ]
        return selected

    @staticmethod
    def _result(entry: dict[str, Any], score: float) -> dict[str, Any]:
        row = entry["row"]
        is_metadata = "content" in row.keys() and "text" not in row.keys()  # noqa: SIM118
        provenance = json.loads(row["provenance"] if is_metadata else row["provenance_json"])
        concise_provenance = [
            {key: item[key] for key in ("path", "kind", "locator", "quote") if key in item}
            for item in provenance[:4]
        ]
        return {
            "source": row["source_name"],
            "source_kind": row["source_kind"],
            "path": row["relative_path"],
            "document_ref": f"{row['source_name']}:{row['relative_path']}",
            "stable_ref": stable_ref(row),
            "chunk_hash": None if is_metadata else row["chunk_hash"],
            "chunk_ordinal": None if is_metadata else row["ordinal"],
            "start_char": None if is_metadata else row["start_char"],
            "end_char": None if is_metadata else row["end_char"],
            "heading_path": next(
                (
                    item.get("metadata", {}).get("heading_path")
                    for item in provenance
                    if item.get("metadata", {}).get("heading_path")
                ),
                None,
            ),
            "indexed_at": row["indexed_at"],
            "source_modified_at": _source_modified_at(row),
            "title": row["title"],
            "text": (f"{row['kind']}: {row['content']}" if is_metadata else row["text"])[:1200],
            "score": round(score, 6),
            "matched_via": entry["matched_via"],
            "provenance": concise_provenance,
            "citation": {
                "source": row["source_name"],
                "external_id": row["external_id"],
                "url": row["source_url"],
                "path": row["relative_path"],
                "content_hash": row["content_hash"],
                "page": row["page_number"] if not is_metadata else None,
                "locators": [item.get("locator") for item in concise_provenance],
            },
            "metadata": json.loads(row["metadata_json"]),
        }

    def _relative_scope(self, scope: str | None) -> str | None:
        if not scope:
            return None
        if scope.startswith("id:"):
            if not scope[3:]:
                raise ValueError("Drive ID scope must include a file or folder ID")
            return scope
        candidate = Path(scope)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("folder scope must be a safe relative path")
        normalized = candidate.as_posix().strip("/")
        if not normalized or normalized == ".":
            raise ValueError("folder scope must identify a relative folder")
        return normalized

    def read(
        self,
        path: str,
        start: int = 0,
        length: int = 12000,
        source: str | None = None,
        around: int | None = None,
        window: int = 2000,
    ) -> dict[str, Any]:
        if start < 0 or not 1 <= length <= 100000:
            raise ValueError("start must be nonnegative and length must be 1..100000")
        if around is not None and (around < 0 or not 1 <= window <= 100000):
            raise ValueError("around must be nonnegative and window must be 1..100000")
        document = self.db.resolve_document(path, source)
        artifact = document["effective_artifact_path"] or document["artifact_path"]
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
        if around is not None:
            if around > len(payload["text"]):
                raise ValueError("around exceeds indexed text length")
            start = max(0, around - window // 2)
            length = window
        end = min(len(payload["text"]), start + length)
        spans = [span for span in payload["spans"] if span["end"] > start and span["start"] < end]
        source_name = str(document["source_name"])
        source_kind = str(document["source_kind"])
        return {
            "document_ref": f"{source_name}:{document['relative_path']}",
            "stable_ref": stable_ref(document),
            "source": source_name,
            "source_kind": source_kind,
            "path": document["relative_path"],
            "external_id": document["external_id"],
            "url": document["source_url"],
            "source_revision": document["source_version"] or None,
            "source_hash": document["content_hash"],
            "content_hash": document["content_hash"],
            "indexed_at": document["indexed_at"],
            "source_modified_at": _source_modified_at(document),
            "authority": "google_drive" if source_kind == "google_drive" else "local_filesystem",
            "text": payload["text"][start:end],
            "start": start,
            "end": end,
            "total_chars": len(payload["text"]),
            "provenance": spans,
            "has_review_corrections": bool(document["effective_artifact_path"]),
        }


def _scope_sql(scope: str | None, column: str) -> tuple[str, list[str]]:
    if not scope:
        return "", []
    escaped = scope.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"AND ({column}=? OR {column} LIKE ? ESCAPE '\\')", [scope, escaped.rstrip("/") + "/%"]


def _filter_sql(
    scope: str | None,
    source: str | None,
    media_type: str | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    values: list[str] = []
    if media_type is not None:
        clauses.append("AND d.media_type=?")
        values.append(media_type)
    after = _date_bound(modified_after)
    before = _date_bound(modified_before)
    if after and before and after >= before:
        raise ValueError("modified_after must precede modified_before")
    for operator, bound in ((">", after), ("<", before)):
        if bound:
            clauses.append(
                "AND (CASE WHEN s.kind='google_drive' THEN "
                "julianday(json_extract(d.metadata_json,'$.modified_time')) "
                "ELSE d.modified_ns / 86400000000000.0 + 2440587.5 END) "
                + operator
                + " julianday(?)"
            )
            values.append(bound)
    if source:
        clauses.append("AND (d.source_id=? OR s.name=?)")
        values.extend((source, source))
    if scope and scope.startswith("id:"):
        clauses.append(
            "AND s.kind='google_drive' AND (d.external_id=? OR EXISTS "
            "(SELECT 1 FROM json_each(d.metadata_json, '$.drive_ancestor_ids') WHERE value=?))"
        )
        values.extend((scope[3:], scope[3:]))
        return " ".join(clauses), values
    scope_sql, scope_values = _scope_sql(scope, "d.relative_path")
    clauses.append(scope_sql)
    values.extend(scope_values)
    return " ".join(clauses), values


def _rrf(lexical_rank: int | None, semantic_rank: int | None, k: int = 60) -> float:
    return sum(1.0 / (k + rank) for rank in (lexical_rank, semantic_rank) if rank is not None)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return (
        sum(a * b for a, b in zip(left, right)) / denominator  # noqa: B905
        if denominator
        else 0.0
    )


def _source_modified_at(row: Any) -> str | None:
    if row["source_kind"] == "google_drive":
        value = json.loads(row["metadata_json"]).get("modified_time")
        return str(value) if value else None
    return datetime.fromtimestamp(row["modified_ns"] / 1e9, UTC).isoformat()


def _numpy() -> Any:
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def _date_bound(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("modified bounds must be ISO 8601 dates/timestamps") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()
