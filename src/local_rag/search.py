from __future__ import annotations

import base64
import binascii
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
from .legal import is_number_query, make_key
from .rerank import Reranker

SEARCH_MODES = ("full_text", "semantic", "hybrid")
MATCH_MODES = ("any", "all", "phrase", "auto")
READ_MAX_CHARS = 40000


class SemanticSearchUnavailable(RuntimeError):
    pass


class SearchEngine:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        embeddings: EmbeddingProvider | None = None,
        query_cache_size: int = 128,
        reranker: Reranker | None = None,
    ):
        self.settings = settings
        self.db = database
        self.embeddings = embeddings
        self.reranker = reranker
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
        match: str = "auto",
        doc_no: str | None = None,
        doc_type: str | None = None,
        valid_on: str | None = None,
    ) -> dict[str, Any]:
        if match not in MATCH_MODES:
            raise ValueError(f"match must be one of: {', '.join(MATCH_MODES)}")
        requested_match = match
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
        filters = (media_type, modified_after, modified_before, doc_type, valid_on)
        _filter_sql(relative_scope, source, *filters)  # Validate even for an empty index.
        warnings: list[str] = []
        output_limit = limit
        if self.reranker is not None:
            limit = max(limit, self.settings.rerank_top_n)  # candidate pool for the reranker
        lexical = (
            self._full_text(
                terms, relative_scope, source, limit, filters, "all" if match == "auto" else match
            )
            if mode != "semantic"
            else {}
        )
        if requested_match == "auto":
            # Results containing every term first, then partial matches (never fewer hits).
            match = "all"
            if mode != "semantic":
                partial = self._full_text(terms, relative_scope, source, limit, filters, "any")
                lexical = self._merge_auto(lexical, partial)
                if len(lexical) == len(partial) and not any(
                    entry.get("all_terms") for entry in lexical.values()
                ):
                    match = "any"
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
        results = self._rerank(query, results, warnings)
        results = self._prioritize_document_number(
            results, query, doc_no, relative_scope, source, filters, output_limit
        )
        results = self._diversify(
            results, per_document, output_limit, relative_scope, source, filters
        )
        return {
            "mode": mode,
            "match": requested_match,
            "effective_match": match,
            "effective_mode": effective_mode,
            "scope": relative_scope,
            "source": source,
            "warnings": warnings,
            "results": results,
        }

    def _rerank(
        self, query: str, results: list[dict[str, Any]], warnings: list[str]
    ) -> list[dict[str, Any]]:
        """Reorder the top candidates with a cross-encoder over each chunk's full text."""
        # `_full_text` carries the untruncated chunk text (result text is cut to 1200 chars).
        full_texts = [str(item.pop("_full_text", item["text"])) for item in results]
        reranker = self.reranker
        if reranker is None or len(results) < 2:
            return results
        count = self.settings.rerank_top_n
        head, tail = results[:count], results[count:]
        texts = full_texts[:count]
        try:
            scores = reranker.rerank(query, texts)
            if len(scores) != len(head):
                raise RuntimeError("reranker returned an unexpected score count")
        except Exception as exc:
            warnings.append(f"rerank unavailable; keeping original order: {exc}")
            return results
        order = sorted(range(len(head)), key=lambda i: (-float(scores[i]), i))
        top = max(float(item["score"]) for item in head)
        floor = float(tail[0]["score"]) if tail else 0.0
        count = len(head)
        # Spread head scores over (floor, top] in the new order; the last head item stays
        # strictly above the first untouched tail item.
        span = max(top - floor, count * 1e-6)
        reordered = []
        for position, index in enumerate(order):
            item = head[index]
            item["reranked"] = True
            item["rerank_score"] = round(float(scores[index]), 6)
            item["score"] = round(floor + span * (count - position) / count, 6)
            reordered.append(item)
        return [*reordered, *tail]

    def _full_text(
        self,
        terms: Sequence[str],
        scope: str | None,
        source: str | None,
        limit: int,
        filters: tuple[str | None, ...] = (None, None, None, None, None),
        match: str = "auto",
    ) -> dict[tuple[str, int], dict[str, Any]]:
        quoted = [f'"{fts_fold(term)}"' for term in terms]
        if match == "phrase":
            fts_query = '"' + " ".join(fts_fold(term) for term in terms) + '"'
        else:
            fts_query = (" AND " if match == "all" else " OR ").join(quoted)
        filter_sql, filter_values = _filter_sql(scope, source, *filters)
        candidate_limit = max(20, limit * 4)
        with self.db.connect() as connection:
            chunks = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                           d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,d.doc_no,
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
                           d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,d.doc_no,
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

    @staticmethod
    def _merge_auto(
        complete: dict[tuple[str, int], dict[str, Any]],
        partial: dict[tuple[str, int], dict[str, Any]],
    ) -> dict[tuple[str, int], dict[str, Any]]:
        merged: dict[tuple[str, int], dict[str, Any]] = {}
        for entry in complete.values():
            entry["all_terms"] = True
        ordered = sorted(complete.items(), key=lambda item: item[1]["lexical_rank"])
        ordered += [
            (key, value)
            for key, value in sorted(partial.items(), key=lambda item: item[1]["lexical_rank"])
            if key not in complete
        ]
        for rank, (key, entry) in enumerate(ordered, 1):
            entry["lexical_rank"] = rank
            merged[key] = entry
        return merged

    def _prioritize_document_number(
        self,
        results: list[dict[str, Any]],
        query: str,
        doc_no: str | None,
        scope: str | None,
        source: str | None,
        filters: tuple[str | None, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        number_query = doc_no is None and is_number_query(query)
        wanted = doc_no if doc_no is not None else (query.strip() if number_query else None)
        if not wanted:
            return results
        key = make_key(wanted)
        filter_sql, values = _filter_sql(scope, source, *filters)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                           d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,d.doc_no,
                           s.name source_name,s.kind source_kind
                    FROM chunks c JOIN documents d ON d.id=c.document_id
                    JOIN sources s ON s.id=d.source_id
                    WHERE d.doc_no_key=? AND s.enabled=1 {filter_sql}
                    ORDER BY d.id,c.ordinal LIMIT ?""",
                [key, *values, limit],
            ).fetchall()
        if not rows:
            return results
        matched = {stable_ref(row) for row in rows}
        priority = [
            self._result({"row": row, "matched_via": "doc_no"}, 1.0) for row in rows if number_query
        ]
        boosted = [item for item in results if item["stable_ref"] in matched]
        rest = [item for item in results if item["stable_ref"] not in matched]
        if not number_query and not boosted:
            boosted = [self._result({"row": row, "matched_via": "doc_no"}, 1.0) for row in rows[:3]]
        return [*priority, *boosted, *rest]

    def _semantic(
        self,
        query: str,
        scope: str | None,
        source: str | None,
        limit: int,
        filters: tuple[str | None, ...] = (None, None, None, None, None),
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
                           d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,d.doc_no,
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
            result = self._result(entry, score)
            row = entry["row"]
            result["_full_text"] = (
                f"{row['kind']}: {row['content']}" if "text" not in row.keys() else row["text"]  # noqa: SIM118
            )
            if "#phu-luc" in str(result["cite_key"]):
                result["score"] = round(result["score"] * 0.85, 6)  # forms rank below articles
            output.append(result)
        return sorted(output, key=lambda item: item["score"], reverse=True)

    def _diversify(
        self,
        results: list[dict[str, Any]],
        per_document: int,
        limit: int,
        scope: str | None,
        source: str | None,
        filters: tuple[str | None, ...],
    ) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        seen: set[str] = set()
        selected = []
        for result in results:
            key = result["body_hash"]
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
                if not result["body_hash"]:
                    continue
                rows = connection.execute(
                    f"""SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                        d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,d.doc_no,
                        s.name source_name,s.kind source_kind FROM chunks c
                        JOIN documents d ON d.id=c.document_id JOIN sources s ON s.id=d.source_id
                        WHERE c.body_hash=? AND s.enabled=1 {filter_sql}
                        ORDER BY d.id,c.ordinal""",
                    [result["body_hash"], *values],
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
        keys = set(row.keys())

        def chunk_field(name: str) -> Any:
            return None if is_metadata or name not in keys else row[name]

        ocr = bool(chunk_field("ocr"))
        raw_path = chunk_field("section_path")
        metadata = json.loads(row["metadata_json"])
        return {
            "cite_key": chunk_field("cite_key"),
            "page_start": chunk_field("page_start"),
            "page_end": chunk_field("page_end"),
            "ocr": ocr,
            "ocr_notice": (
                "OCR, chưa kiểm chứng: mở trang gốc trước khi trích dẫn" if ocr else None
            ),
            "section_path": json.loads(raw_path) if raw_path else None,
            "doc_no": row["doc_no"] if "doc_no" in keys else None,
            "source": row["source_name"],
            "source_kind": row["source_kind"],
            "path": row["relative_path"],
            "document_ref": f"{row['source_name']}:{row['relative_path']}",
            "stable_ref": stable_ref(row),
            "chunk_hash": None if is_metadata else row["chunk_hash"],
            "body_hash": None if is_metadata else row["body_hash"] or row["chunk_hash"],
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
                "cite_key": chunk_field("cite_key"),
                "page_start": chunk_field("page_start"),
                "page_end": chunk_field("page_end"),
                "doc_no": row["doc_no"] if "doc_no" in keys else None,
                "locators": [item.get("locator") for item in concise_provenance],
            },
            "layout_flags": metadata.get("layout_flags", []),
            "metadata": metadata,
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

    def get_chunk(
        self,
        cite_key: str | None = None,
        document_ref: str | None = None,
        ordinal: int | None = None,
        neighbors: int = 0,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Return one chunk by cite_key or document_ref+ordinal, with optional neighbours."""
        if not 0 <= neighbors <= 3:
            raise ValueError("neighbors must be between 0 and 3")
        if bool(cite_key) == bool(document_ref):
            raise ValueError("provide exactly one of cite_key or document_ref with ordinal")
        columns = """SELECT c.*,d.relative_path,d.title,d.content_hash,d.metadata_json,
                     d.source_id,d.external_id,d.source_url,d.indexed_at,d.modified_ns,d.doc_no,
                     s.name source_name,s.kind source_kind
                     FROM chunks c JOIN documents d ON d.id=c.document_id
                     JOIN sources s ON s.id=d.source_id"""
        source_sql = "AND (d.source_id=? OR s.name=?)" if source else ""
        source_values = [source, source] if source else []
        warnings: list[str] = []
        with self.db.connect() as connection:
            if cite_key:
                rows = connection.execute(
                    f"{columns} WHERE c.cite_key=? AND s.enabled=1 "
                    f"AND d.trashed_at IS NULL {source_sql} ORDER BY d.id,c.ordinal",
                    [cite_key, *source_values],
                ).fetchall()
                if not rows:
                    raise ValueError(f"no indexed chunk has cite_key: {cite_key}")
                if len({row["document_id"] for row in rows}) > 1:
                    warnings.append(
                        "cite_key matches several documents; returning the first. "
                        "Use document_ref with ordinal to choose another."
                    )
                row = rows[0]
            else:
                if ordinal is None or ordinal < 0:
                    raise ValueError("ordinal must be a nonnegative integer")
                document = self.db.resolve_document(str(document_ref), source)
                found = connection.execute(
                    f"{columns} WHERE c.document_id=? AND c.ordinal=? AND s.enabled=1 "
                    "AND d.trashed_at IS NULL",
                    [document["id"], ordinal],
                ).fetchone()
                if found is None:
                    raise ValueError("no chunk with that ordinal in the document")
                row = found
            around = connection.execute(
                f"{columns} WHERE c.document_id=? AND c.ordinal BETWEEN ? AND ? "
                "AND c.ordinal<>? ORDER BY c.ordinal",
                [
                    row["document_id"],
                    row["ordinal"] - neighbors,
                    row["ordinal"] + neighbors,
                    row["ordinal"],
                ],
            ).fetchall()

        def full(item: Any) -> dict[str, Any]:
            result = self._result({"row": item, "matched_via": "chunk"}, 1.0)
            result["text"] = item["text"]
            return result

        return {
            "chunk": full(row),
            "neighbors": [full(item) for item in around],
            "warnings": warnings,
        }

    def read(
        self,
        path: str,
        start: int = 0,
        length: int = 12000,
        source: str | None = None,
        around: int | None = None,
        window: int = 2000,
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        if start < 0 or not 1 <= length <= READ_MAX_CHARS:
            raise ValueError(f"start must be nonnegative and length must be 1..{READ_MAX_CHARS}")
        if around is not None and (around < 0 or not 1 <= window <= READ_MAX_CHARS):
            raise ValueError(f"around must be nonnegative and window must be 1..{READ_MAX_CHARS}")
        document = self.db.resolve_document(path, source, include_trashed)
        artifact = document["effective_artifact_path"] or document["artifact_path"]
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
        if around is not None:
            if around > len(payload["text"]):
                raise ValueError("around exceeds indexed text length")
            start = max(0, around - window // 2)
            length = window
        total_chars = len(payload["text"])
        end = min(total_chars, start + length)
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
            "next_start": end if end < total_chars else None,
            "total_chars": total_chars,
            "provenance": spans,
            "has_review_corrections": bool(document["effective_artifact_path"]),
            "layout_flags": json.loads(document["metadata_json"]).get("layout_flags", []),
            "layout_pages": json.loads(document["metadata_json"]).get("layout_pages", []),
            **({"trashed_at": document["trashed_at"]} if document["trashed_at"] else {}),
        }

    def list_documents(
        self,
        source: str | None = None,
        folder: str | None = None,
        media_type: str | None = None,
        doc_type: str | None = None,
        modified_after: str | None = None,
        modified_before: str | None = None,
        include_trashed: bool = False,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List indexed documents with stable keyset pagination ordered by (source_id, id)."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        after = _decode_cursor(cursor) if cursor else None
        filters, values = _filter_sql(
            self._relative_scope(folder),
            source,
            media_type,
            modified_after,
            modified_before,
            doc_type,
            include_trashed=include_trashed,
        )
        base = "FROM documents d JOIN sources s ON s.id=d.source_id WHERE s.enabled=1 " + filters
        page_sql, page_values = "", []
        if after:
            page_sql = " AND (d.source_id>? OR (d.source_id=? AND d.id>?))"
            page_values = [after[0], after[0], after[1]]
        with self.db.connect() as connection:
            total = connection.execute(f"SELECT count(*) {base}", values).fetchone()[0]
            rows = connection.execute(
                "SELECT d.*,s.name source_name,s.kind source_kind,"
                "(SELECT count(*) FROM chunks c WHERE c.document_id=d.id) chunk_count "
                f"{base}{page_sql} ORDER BY d.source_id,d.id LIMIT ?",
                [*values, *page_values, limit + 1],
            ).fetchall()
        page = rows[:limit]
        next_cursor = (
            _encode_cursor(page[-1]["source_id"], page[-1]["id"]) if len(rows) > limit else None
        )
        return {
            "documents": [_listing_item(row) for row in page],
            "next_cursor": next_cursor,
            "total": total,
        }


def _encode_cursor(source_id: str, document_id: int) -> str:
    raw = json.dumps({"s": source_id, "i": document_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if isinstance(data, dict) and isinstance(data["s"], str) and type(data["i"]) is int:
            return data["s"], data["i"]
    except (ValueError, KeyError, binascii.Error):
        pass
    raise ValueError("invalid cursor: restart the listing without a cursor")


def _iso_utc(value: str | None) -> str | None:
    if value and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", value):
        return value.replace(" ", "T") + "Z"
    return value


def _listing_item(row: Any) -> dict[str, Any]:
    return {
        "stable_ref": stable_ref(row),
        "source": row["source_name"],
        "path": row["relative_path"],
        "title": row["title"],
        "media_type": row["media_type"],
        "doc_no": row["doc_no"],
        "url": row["source_url"],
        "indexed_at": _iso_utc(row["indexed_at"]),
        "source_modified_at": _source_modified_at(row),
        "trashed": bool(row["trashed_at"]),
        "chunk_count": row["chunk_count"],
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
    doc_type: str | None = None,
    valid_on: str | None = None,
    *,
    include_trashed: bool = False,
) -> tuple[str, list[str]]:
    clauses: list[str] = [] if include_trashed else ["AND d.trashed_at IS NULL"]
    values: list[str] = []
    if doc_type:
        clauses.append("AND ('-' || d.doc_no_key || '-') LIKE ?")
        values.append(f"%-{make_key(doc_type)}-%")
    if valid_on:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valid_on):
            raise ValueError("valid_on must be YYYY-MM-DD")
        clauses.append(
            "AND EXISTS (SELECT 1 FROM document_validity v WHERE v.document_id=d.id "
            "AND v.effective_from IS NOT NULL AND v.effective_from<=? "
            "AND (v.effective_to IS NULL OR v.effective_to>?))"
        )
        values.extend((valid_on, valid_on))
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
