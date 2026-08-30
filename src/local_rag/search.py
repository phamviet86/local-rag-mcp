from __future__ import annotations

import json
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
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

    def search(
        self,
        query: str,
        limit: int = 8,
        scope: str | None = None,
        mode: str = "hybrid",
        source: str | None = None,
    ) -> dict[str, Any]:
        if mode not in SEARCH_MODES:
            raise ValueError(f"mode must be one of: {', '.join(SEARCH_MODES)}")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("query must contain searchable terms")
        relative_scope = self._relative_scope(scope)
        warnings: list[str] = []
        lexical = (
            self._full_text(terms, relative_scope, source, limit) if mode != "semantic" else {}
        )
        semantic: dict[int, tuple[int, float, Any]] = {}
        if mode != "full_text":
            try:
                semantic = self._semantic(query, relative_scope, source, limit)
            except Exception as exc:
                if mode == "semantic":
                    raise SemanticSearchUnavailable(f"semantic search unavailable: {exc}") from exc
                warnings.append(f"semantic search unavailable; using full_text: {exc}")
        effective_mode = mode
        if mode == "hybrid" and not semantic:
            effective_mode = "full_text"
        results = self._combine(lexical, semantic, limit, effective_mode)
        return {
            "mode": mode,
            "effective_mode": effective_mode,
            "scope": relative_scope,
            "source": source,
            "warnings": warnings,
            "results": results,
        }

    def _full_text(
        self, terms: Sequence[str], scope: str | None, source: str | None, limit: int
    ) -> dict[tuple[str, int], dict[str, Any]]:
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        filter_sql, filter_values = _filter_sql(scope, source)
        candidate_limit = max(20, limit * 4)
        with self.db.connect() as connection:
            chunks = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                           d.external_id,d.source_url,s.name source_name,s.kind source_kind
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
                           d.external_id,d.source_url,s.name source_name,s.kind source_kind
                    FROM metadata_fts m JOIN documents d ON d.id=CAST(m.document_id AS INTEGER)
                    JOIN sources s ON s.id=d.source_id
                    WHERE metadata_fts MATCH ? AND s.enabled=1 {filter_sql}
                    ORDER BY bm25(metadata_fts) LIMIT ?""",
                [fts_query, *filter_values, candidate_limit],
            ).fetchall()
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
        self, query: str, scope: str | None, source: str | None, limit: int
    ) -> dict[int, tuple[int, float, Any]]:
        if self.embeddings is None:
            raise RuntimeError("no embedding provider is configured")
        provider, model = cache_identity(self.embeddings)
        query_vector = self._query_vector(query, provider, model)
        filter_sql, filter_values = _filter_sql(scope, source)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                           d.external_id,d.source_url,s.name source_name,s.kind source_kind,
                           v.vector_json
                    FROM chunks c JOIN documents d ON d.id=c.document_id
                    JOIN sources s ON s.id=d.source_id
                    JOIN vectors v ON v.chunk_hash=c.chunk_hash
                    WHERE v.provider=? AND v.model=? AND s.enabled=1 {filter_sql}""",
                [provider, model, *filter_values],
            ).fetchall()
        if not rows:
            raise RuntimeError("no cached vectors exist for the configured provider and model")
        scored = sorted(
            ((_cosine(query_vector, json.loads(row["vector_json"])), row) for row in rows),
            key=lambda item: item[0],
            reverse=True,
        )[: max(20, limit * 4)]
        return {
            int(row["id"]): (rank, max(0.0, (score + 1) / 2), row)
            for rank, (score, row) in enumerate(scored, 1)
        }

    def _query_vector(self, query: str, provider: str, model: str) -> Sequence[float]:
        key = provider, model, query
        with self._cache_lock:
            cached = self._query_cache.get(key)
            if cached is not None:
                self._query_cache.move_to_end(key)
                return cached
        vector = self.embeddings.embed([query])[0]
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
        return sorted(output, key=lambda item: item["score"], reverse=True)[:limit]

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
                "page": row["page_number"] if "page_number" in row else None,  # noqa: SIM401
                "locators": [item.get("locator") for item in concise_provenance],
            },
            "metadata": json.loads(row["metadata_json"]),
        }

    def _relative_scope(self, scope: str | None) -> str | None:
        if not scope:
            return None
        candidate = Path(scope)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("folder scope must be a safe relative path")
        return candidate.as_posix().strip("./")

    def read(
        self,
        path: str,
        start: int = 0,
        length: int = 12000,
        source: str | None = None,
    ) -> dict[str, Any]:
        document = self.db.resolve_document(path, source)
        artifact = document["effective_artifact_path"] or document["artifact_path"]
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
        end = min(len(payload["text"]), start + length)
        spans = [span for span in payload["spans"] if span["end"] > start and span["start"] < end]
        return {
            "path": document["relative_path"],
            "content_hash": document["content_hash"],
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


def _filter_sql(scope: str | None, source: str | None) -> tuple[str, list[str]]:
    clauses, values = [], []
    if source:
        clauses.append("AND (d.source_id=? OR s.name=?)")
        values.extend((source, source))
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
