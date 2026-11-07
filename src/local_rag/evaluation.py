"""Retrieval evaluation: recall@k of gold cite_keys over a JSONL question set."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

KS = (1, 3, 10)


def load_questions(path: Path) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"line {number}: invalid JSON") from exc
        gold = item.get("gold")
        if (
            not isinstance(item, dict)
            or not item.get("id")
            or not item.get("question")
            or not isinstance(gold, list)
            or not gold
            or not all(isinstance(key, str) for key in gold)
        ):
            raise ValueError(f"line {number}: need id, question and a non-empty gold list")
        questions.append(item)
    if not questions:
        raise ValueError("question file contains no questions")
    return questions


def _matches(cite_key: str | None, gold: str) -> bool:
    # A gold Điều also accepts any of its khoản chunks.
    return bool(cite_key) and (cite_key == gold or str(cite_key).startswith(gold + "-khoan-"))


def _rank(results: list[dict[str, Any]], gold: list[str]) -> int | None:
    for position, result in enumerate(results, 1):
        if any(_matches(result.get("cite_key"), key) for key in gold):
            return position
    return None


def _ndcg(results: list[dict[str, Any]], gold: list[str]) -> float:
    """Binary nDCG@10 over all gold keys; each gold key is credited by at most one result."""
    remaining = list(gold)
    dcg = 0.0
    for position, result in enumerate(results[:10]):
        match = next((key for key in remaining if _matches(result.get("cite_key"), key)), None)
        if match is not None:
            remaining.remove(match)
            dcg += 1 / math.log2(position + 2)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(gold), 10)))
    return dcg / ideal if ideal else 0.0


def evaluate(
    questions: list[dict[str, Any]],
    search: Callable[[str], list[dict[str, Any]]],
    exists: Callable[[str], bool],
) -> dict[str, Any]:
    """Run each question; report recall@1/3/10, MRR@10, nDCG@10 and failure groups."""
    ranks: list[int | None] = []
    gains: list[float] = []
    misses: list[dict[str, Any]] = []
    for item in questions:
        gold = list(item["gold"])
        results = search(str(item["question"]))
        rank = _rank(results, gold)
        ranks.append(rank)
        gains.append(_ndcg(results, gold))
        if rank is not None and rank <= max(KS):
            continue
        if rank is not None:
            group = "ranked_below_10"
        elif not any(exists(key) for key in gold):
            group = "gold_not_indexed"
        elif not results:
            group = "no_results"
        else:
            documents = {str(key).split("#", 1)[0] for key in gold}
            found = {str(r.get("cite_key") or "").split("#", 1)[0] for r in results}
            group = "wrong_chunk_right_document" if documents & found else "wrong_document"
        misses.append(
            {
                "id": item["id"],
                "question": item["question"],
                "gold": gold,
                "group": group,
                "top": [r.get("cite_key") for r in results[:3]],
            }
        )

    def recall(indices: list[int], k: int) -> float:
        hits = sum(1 for i in indices if ranks[i] is not None and (ranks[i] or 0) <= k)
        return round(hits / len(indices), 4) if indices else 0.0

    def mrr(indices: list[int]) -> float:
        total = sum(1 / (ranks[i] or 1) for i in indices if ranks[i] and (ranks[i] or 0) <= 10)
        return round(total / len(indices), 4) if indices else 0.0

    def ndcg(indices: list[int]) -> float:
        return round(sum(gains[i] for i in indices) / len(indices), 4) if indices else 0.0

    everything = list(range(len(questions)))
    by_type: dict[str, dict[str, Any]] = {}
    for key in ("type", "diacritics"):
        for value in sorted({str(q.get(key)) for q in questions if q.get(key) is not None}):
            members = [i for i, q in enumerate(questions) if str(q.get(key)) == value]
            by_type[f"{key}={value}"] = {
                "count": len(members),
                **{f"recall@{k}": recall(members, k) for k in KS},
                "mrr@10": mrr(members),
                "ndcg@10": ndcg(members),
            }
    groups: dict[str, int] = {}
    for miss in misses:
        groups[miss["group"]] = groups.get(miss["group"], 0) + 1
    return {
        "count": len(questions),
        **{f"recall@{k}": recall(everything, k) for k in KS},
        "mrr@10": mrr(everything),
        "ndcg@10": ndcg(everything),
        "by_group": by_type,
        "failure_groups": groups,
        "misses": misses,
    }
