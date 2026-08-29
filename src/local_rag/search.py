import json
import math
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .db import Database
from .embeddings import EmbeddingProvider

SEARCH_MODES = ("full_text", "semantic", "hybrid")


class SemanticSearchUnavailable(RuntimeError):
    pass


class SearchEngine:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        embeddings: Optional[EmbeddingProvider] = None,
        query_cache_size: int = 128,
    ):
        self.settings = settings
        self.db = database
        self.embeddings = embeddings
        self.query_cache_size = query_cache_size
        self._query_cache: OrderedDict[Tuple[str, str, str], Sequence[float]] = OrderedDict()
        self._cache_lock = threading.Lock()

    def search(
        self,
        query: str,
        limit: int = 8,
        scope: Optional[str] = None,
        mode: str = "hybrid",
    ) -> Dict[str, Any]:
        if mode not in SEARCH_MODES:
            raise ValueError(f"mode must be one of: {', '.join(SEARCH_MODES)}")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("query must contain searchable terms")
        relative_scope = self._relative_scope(scope)
        warnings: List[str] = []
        lexical = self._full_text(terms, relative_scope, limit) if mode != "semantic" else {}
        semantic: Dict[int, Tuple[float, Any]] = {}
        if mode != "full_text":
            try:
                semantic = self._semantic(query, relative_scope, limit)
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
            "warnings": warnings,
            "results": results,
        }

    def _full_text(
        self, terms: Sequence[str], scope: Optional[str], limit: int
    ) -> Dict[Tuple[str, int], Dict[str, Any]]:
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        scope_sql, scope_values = _scope_sql(scope, "d.relative_path")
        candidate_limit = max(20, limit * 4)
        with self.db.connect() as connection:
            chunks = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json
                    FROM chunks_fts JOIN chunks c ON c.id=CAST(chunks_fts.chunk_id AS INTEGER)
                    JOIN documents d ON d.id=c.document_id
                    WHERE chunks_fts MATCH ? {scope_sql}
                    ORDER BY bm25(chunks_fts) LIMIT ?""",
                [fts_query, *scope_values, candidate_limit],
            ).fetchall()
            metadata = connection.execute(
                f"""SELECT m.rowid metadata_id,m.content,m.kind,m.provenance,
                           d.id document_id,d.relative_path,d.title,d.content_hash,d.metadata_json
                    FROM metadata_fts m JOIN documents d ON d.id=CAST(m.document_id AS INTEGER)
                    WHERE metadata_fts MATCH ? {scope_sql}
                    ORDER BY bm25(metadata_fts) LIMIT ?""",
                [fts_query, *scope_values, candidate_limit],
            ).fetchall()
        results: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for rank, row in enumerate(chunks):
            results[("chunk", int(row["id"]))] = {
                "row": row,
                "lexical": 1 / (rank + 1),
                "semantic": 0.0,
                "matched_via": "content",
            }
        offset = len(chunks)
        for rank, row in enumerate(metadata):
            key = ("metadata", int(row["metadata_id"]))
            results[key] = {
                "row": row,
                "lexical": 1 / (offset + rank + 1),
                "semantic": 0.0,
                "matched_via": row["kind"],
            }
        return results

    def _semantic(
        self, query: str, scope: Optional[str], limit: int
    ) -> Dict[int, Tuple[float, Any]]:
        if self.embeddings is None:
            raise RuntimeError("no embedding provider is configured")
        provider, model = self.embeddings.identity
        query_vector = self._query_vector(query, provider, model)
        scope_sql, scope_values = _scope_sql(scope, "d.relative_path")
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,v.vector_json
                    FROM chunks c JOIN documents d ON d.id=c.document_id
                    JOIN vectors v ON v.chunk_hash=c.chunk_hash
                    WHERE v.provider=? AND v.model=? {scope_sql}""",
                [provider, model, *scope_values],
            ).fetchall()
        if not rows:
            raise RuntimeError("no cached vectors exist for the configured provider and model")
        scored = sorted(
            ((_cosine(query_vector, json.loads(row["vector_json"])), row) for row in rows),
            key=lambda item: item[0],
            reverse=True,
        )[: max(20, limit * 4)]
        return {int(row["id"]): (max(0.0, (score + 1) / 2), row) for score, row in scored}

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
        lexical: Dict[Tuple[str, int], Dict[str, Any]],
        semantic: Dict[int, Tuple[float, Any]],
        limit: int,
        mode: str,
    ) -> List[Dict[str, Any]]:
        candidates = dict(lexical)
        for chunk_id, (score, row) in semantic.items():
            key = ("chunk", chunk_id)
            entry = candidates.setdefault(
                key,
                {
                    "row": row,
                    "lexical": 0.0,
                    "semantic": 0.0,
                    "matched_via": "semantic",
                },
            )
            entry["semantic"] = score
            if entry["matched_via"] == "content":
                entry["matched_via"] = "content+semantic"
        output = []
        for entry in candidates.values():
            lexical_score = float(entry["lexical"])
            semantic_score = float(entry["semantic"])
            if mode == "semantic":
                score = semantic_score
            elif mode == "hybrid":
                score = 0.65 * lexical_score + 0.35 * semantic_score
            else:
                score = lexical_score
            output.append(self._result(entry, score))
        return sorted(output, key=lambda item: item["score"], reverse=True)[:limit]

    @staticmethod
    def _result(entry: Dict[str, Any], score: float) -> Dict[str, Any]:
        row = entry["row"]
        is_metadata = "content" in row.keys() and "text" not in row.keys()
        provenance = json.loads(row["provenance"] if is_metadata else row["provenance_json"])
        concise_provenance = [
            {key: item[key] for key in ("path", "kind", "locator", "quote") if key in item}
            for item in provenance[:4]
        ]
        return {
            "path": row["relative_path"],
            "title": row["title"],
            "text": (f"{row['kind']}: {row['content']}" if is_metadata else row["text"])[:1200],
            "score": round(score, 6),
            "matched_via": entry["matched_via"],
            "provenance": concise_provenance,
            "metadata": json.loads(row["metadata_json"]),
        }

    def _relative_scope(self, scope: Optional[str]) -> Optional[str]:
        path = self.settings.scope(scope)
        return path.relative_to(self.settings.root).as_posix() if path is not None else None

    def read(self, path: str, start: int = 0, length: int = 12000) -> Dict[str, Any]:
        document = self.db.resolve_document(path)
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


def _scope_sql(scope: Optional[str], column: str) -> Tuple[str, List[str]]:
    if not scope:
        return "", []
    escaped = scope.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"AND ({column}=? OR {column} LIKE ? ESCAPE '\\')", [scope, escaped.rstrip("/") + "/%"]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0
