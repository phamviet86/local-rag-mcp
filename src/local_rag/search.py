import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import Settings
from .db import Database
from .embeddings import EmbeddingProvider
from .models import SearchHit


class SearchEngine:
    def __init__(
        self, settings: Settings, database: Database, embeddings: Optional[EmbeddingProvider] = None
    ):
        self.settings, self.db, self.embeddings = settings, database, embeddings

    def search(self, query: str, limit: int = 8, scope: Optional[str] = None) -> List[SearchHit]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        scope_path = self.settings.scope(scope)
        relative_scope = (
            scope_path.relative_to(self.settings.root).as_posix()
            if scope_path is not None
            else None
        )
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms:
            raise ValueError("query must contain searchable terms")
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        where, parameters = "chunks_fts MATCH ?", [fts_query]
        if relative_scope:
            where += " AND (d.relative_path=? OR d.relative_path LIKE ?)"
            parameters.extend([relative_scope, relative_scope.rstrip("/") + "/%"])
        parameters.append(max(20, limit * 4))
        with self.db.connect() as connection:
            lexical_rows = connection.execute(
                f"""SELECT c.*,d.path,d.relative_path,d.title,d.content_hash,d.metadata_json
                    FROM chunks_fts JOIN chunks c ON c.id=CAST(chunks_fts.chunk_id AS INTEGER)
                    JOIN documents d ON d.id=c.document_id
                    WHERE {where} ORDER BY bm25(chunks_fts) LIMIT ?""",
                parameters,
            ).fetchall()
        candidates: Dict[int, Dict[str, Any]] = {
            int(row["id"]): {"row": row, "lexical": 1 / (rank + 1), "semantic": 0.0}
            for rank, row in enumerate(lexical_rows)
        }
        if self.embeddings is not None:
            provider, model = self.embeddings.identity
            query_vector = self.embeddings.embed([query])[0]
            sql = """SELECT c.*,d.path,d.relative_path,d.title,d.content_hash,d.metadata_json,v.vector_json
                     FROM chunks c JOIN documents d ON d.id=c.document_id
                     JOIN vectors v ON v.chunk_hash=c.chunk_hash
                     WHERE v.provider=? AND v.model=?"""
            values: List[Any] = [provider, model]
            if relative_scope:
                sql += " AND (d.relative_path=? OR d.relative_path LIKE ?)"
                values.extend([relative_scope, relative_scope.rstrip("/") + "/%"])
            with self.db.connect() as connection:
                vector_rows = connection.execute(sql, values).fetchall()
            scored = sorted(
                (
                    (_cosine(query_vector, json.loads(row["vector_json"])), row)
                    for row in vector_rows
                ),
                reverse=True,
                key=lambda item: item[0],
            )[: max(20, limit * 4)]
            for similarity, row in scored:
                entry = candidates.setdefault(
                    int(row["id"]), {"row": row, "lexical": 0.0, "semantic": 0.0}
                )
                entry["semantic"] = max(0.0, (similarity + 1) / 2)
        hits = []
        for entry in candidates.values():
            row = entry["row"]
            lexical, semantic = float(entry["lexical"]), float(entry["semantic"])
            weight = 0.65 if self.embeddings is not None else 1.0
            hits.append(
                SearchHit(
                    path=row["relative_path"],
                    title=row["title"],
                    document_hash=row["content_hash"],
                    chunk_ordinal=row["ordinal"],
                    text=row["text"],
                    score=weight * lexical + (1 - weight) * semantic,
                    lexical_score=lexical,
                    semantic_score=semantic,
                    provenance=json.loads(row["provenance_json"]),
                    metadata=json.loads(row["metadata_json"]),
                )
            )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]

    def read(self, path: str, start: int = 0, length: int = 12000) -> Dict[str, Any]:
        document = self.db.resolve_document(path)
        payload = json.loads(Path(document["artifact_path"]).read_text(encoding="utf-8"))
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
        }


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    denominator = math.sqrt(sum(v * v for v in left)) * math.sqrt(sum(v * v for v in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0
