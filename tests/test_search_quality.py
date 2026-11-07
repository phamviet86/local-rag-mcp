from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from local_rag.config import Settings
from local_rag.embeddings import cache_identity
from local_rag.evaluation import evaluate
from local_rag.service import MultiSourceRAG


def test_mrr_and_ndcg_on_hand_built_ranks() -> None:
    questions = [
        {"id": "a", "question": "a", "gold": ["g1"], "type": "x"},
        {"id": "b", "question": "b", "gold": ["g2"], "type": "x"},
        {"id": "c", "question": "c", "gold": ["g3"], "type": "y"},
        {"id": "d", "question": "d", "gold": ["g4"], "type": "y"},
    ]
    runs = {
        "a": ["g1", "z"],
        "b": ["z", "g2-khoan-1"],
        "c": ["z"],
        "d": [*(f"z{i}" for i in range(10)), "g4"],  # rank 11 does not count at 10
    }
    report = evaluate(
        questions,
        lambda q: [{"cite_key": key} for key in runs[q]],
        lambda key: True,
    )
    assert report["mrr@10"] == round((1 + 0.5) / 4, 4)
    assert report["ndcg@10"] == round((1 + 1 / math.log2(3)) / 4, 4)
    assert report["by_group"]["type=x"]["mrr@10"] == 0.75
    assert report["by_group"]["type=x"]["ndcg@10"] == round((1 + 1 / math.log2(3)) / 2, 4)
    assert report["by_group"]["type=y"]["mrr@10"] == 0.0
    assert report["by_group"]["type=y"]["ndcg@10"] == 0.0


class MarkerReranker:
    """Scores texts by how many PRIORITY markers the full chunk text holds."""

    identity = ("fake", "marker")

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        self.seen.append(list(texts))
        return [float(text.count("PRIORITY")) for text in texts]


class BrokenReranker:
    identity = ("fake", "broken")

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        raise RuntimeError("model exploded")


def _service(
    tmp_path: Path, files: dict[str, str], reranker: object | None = None, **overrides: object
) -> MultiSourceRAG:
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    options: dict[str, object] = {
        "chunk_strategy": "window",
        "chunk_chars": 5000,
        "chunk_overlap": 100,
        **overrides,
    }
    settings = Settings(root=home, home=home, **options)  # type: ignore[arg-type]
    service = MultiSourceRAG(settings, reranker=reranker)  # type: ignore[arg-type]
    service.add_local_source("notes", root)
    service.reconcile()
    return service


def _paths(response: dict[str, object]) -> list[str]:
    return [str(hit["path"]) for hit in response["results"]]  # type: ignore[attr-defined]


FILES = {
    "short.txt": "zebra zebra zebra",
    "long.txt": "zebra " + "filler " * 300 + "PRIORITY PRIORITY",  # marker beyond 1200 chars
    "mid.txt": "zebra mid note",
}


def test_reranker_uses_full_chunk_text_not_truncated_result(tmp_path: Path) -> None:
    plain = _service(tmp_path / "a", FILES)
    baseline = plain.search("zebra", mode="full_text")
    assert _paths(baseline)[0] != "long.txt"
    assert all("reranked" not in hit for hit in baseline["results"])

    reranker = MarkerReranker()
    service = _service(tmp_path / "b", FILES, reranker)
    response = service.search("zebra", mode="full_text")
    assert _paths(response)[0] == "long.txt"
    first = response["results"][0]
    assert first["reranked"] is True
    assert len(first["text"]) == 1200 and "PRIORITY" not in first["text"]
    assert any("PRIORITY" in text and len(text) > 1200 for text in reranker.seen[0])
    assert all("_full_text" not in hit for hit in response["results"])
    scores = [hit["score"] for hit in response["results"]]
    assert scores == sorted(scores, reverse=True)
    assert response["warnings"] == []


def test_reranker_failure_warns_and_keeps_original_order(tmp_path: Path) -> None:
    baseline = _paths(_service(tmp_path / "a", FILES).search("zebra", mode="full_text"))
    response = _service(tmp_path / "b", FILES, BrokenReranker()).search("zebra", mode="full_text")
    assert _paths(response) == baseline
    assert any("rerank unavailable" in warning for warning in response["warnings"])
    assert all("reranked" not in hit for hit in response["results"])


def test_rerank_top_n_boundary(tmp_path: Path) -> None:
    baseline = _paths(_service(tmp_path / "a", FILES).search("zebra", mode="full_text"))
    reranker = MarkerReranker()
    service = _service(tmp_path / "b", FILES, reranker, rerank_top_n=2)
    response = service.search("zebra", mode="full_text")
    assert len(reranker.seen[0]) == 2
    flags = [bool(hit.get("reranked")) for hit in response["results"]]
    assert flags == [True, True, False]
    # The candidate beyond top_n keeps its slot even if the reranker would have promoted it.
    assert _paths(response)[2] == baseline[2]


def test_rerank_settings_round_trip_and_validation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    Settings(
        root=home,
        home=home,
        rerank_provider="local",
        rerank_model="some/model",
        rerank_top_n=12,
    ).save()
    with patch.dict("os.environ", {}, clear=False):
        loaded = Settings.load(home)
    assert (loaded.rerank_provider, loaded.rerank_model, loaded.rerank_top_n) == (
        "local",
        "some/model",
        12,
    )
    defaults = Settings(root=home, home=home)
    assert (defaults.rerank_provider, defaults.rerank_model, defaults.rerank_top_n) == (
        "none",
        "BAAI/bge-reranker-v2-m3",
        30,
    )
    with pytest.raises(ValueError):
        Settings(root=home, home=home, rerank_provider="remote")
    with pytest.raises(ValueError):
        Settings(root=home, home=home, rerank_top_n=0)


def test_doctor_and_status_report_reranker(tmp_path: Path) -> None:
    none_service = _service(tmp_path / "a", {"n.txt": "zebra"})
    assert none_service.status()["reranker"]["provider"] == "none"
    assert none_service.doctor()["checks"]["reranker"]["status"] == "ok"

    fake = _service(tmp_path / "b", {"n.txt": "zebra"}, MarkerReranker())
    status = fake.status()["reranker"]
    assert status["provider"] == "fake" and status["model"] == "marker" and status["available"]

    with patch("local_rag.rerank.find_spec", return_value=None):
        missing = _service(tmp_path / "c", {"n.txt": "zebra"}, rerank_provider="local")
        report = missing.doctor()
        assert missing.status()["reranker"]["available"] is False
        assert report["checks"]["reranker"]["status"] == "warning"
        assert {"command": "pip install 'phamviet-local-rag-mcp[rerank]'"} in report["actions"]
        # Search still works without the optional dependency or model.
        assert missing.search("zebra", mode="full_text")["results"]


def test_window_chunks_carry_context_prefix_with_body_offsets(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        {"note.txt": "orchard harvest " * 100},
        chunk_chars=100,
        chunk_overlap=10,
    )
    hits = service.search("orchard", mode="full_text", limit=50)["results"]
    assert hits
    document = service.read("notes:note.txt")["text"]
    for hit in hits:
        context, body = hit["text"].split("\n", 1)
        assert context == "note | note.txt"
        assert document[hit["start_char"] : hit["end_char"]] == body
        around = service.read("notes:note.txt", around=hit["start_char"], window=60)
        assert around["text"] == document[around["start"] : around["end"]]
        assert around["start"] <= hit["start_char"] < around["end"]
    chunk = service.get_chunk(document_ref="notes:note.txt", ordinal=0)
    assert chunk["chunk"]["text"].startswith("note | note.txt\n")


def test_ndcg_counts_every_gold_hit_once() -> None:
    questions = [{"id": "m", "question": "m", "gold": ["a", "b", "c"]}]
    # a at rank 1, b matched via a khoan chunk at rank 3 (a second khoan of b earns nothing).
    results = [
        {"cite_key": "a"},
        {"cite_key": "z"},
        {"cite_key": "b-khoan-1"},
        {"cite_key": "b-khoan-2"},
    ]
    report = evaluate(questions, lambda q: results, lambda key: True)
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    expected = (1 + 1 / math.log2(4)) / ideal
    assert report["ndcg@10"] == round(expected, 4)
    assert report["mrr@10"] == 1.0


def test_rerank_pool_is_wider_than_limit_and_output_is_cut(tmp_path: Path) -> None:
    files = {f"d{i:02}.txt": f"zebra number {i}" for i in range(40)}
    reranker = MarkerReranker()
    service = _service(tmp_path / "a", files, reranker, rerank_top_n=30)
    response = service.search("zebra", limit=2, mode="full_text")
    assert len(response["results"]) == 2
    assert len(reranker.seen[0]) == 30
    plain = _service(tmp_path / "b", files).search("zebra", limit=2, mode="full_text")
    assert len(plain["results"]) == 2


def test_reranked_head_stays_above_tail(tmp_path: Path) -> None:
    class Reverse:
        identity = ("fake", "reverse")

        def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
            return [float(-i) for i in range(len(texts))]

    service = _service(tmp_path, FILES, Reverse(), rerank_top_n=2)
    results = service.search("zebra", mode="full_text")["results"]
    scores = [hit["score"] for hit in results]
    assert scores[1] > scores[2]
    assert scores == sorted(scores, reverse=True)


class RecordingEmbeddings:
    identity = ("rec", "m1")

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[1.0, float(len(text) % 7)] for text in texts]


def test_reextract_embeds_context_prefix_and_copies_get_separate_vectors(tmp_path: Path) -> None:
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    for name in ("one.txt", "copy.txt"):
        (root / name).write_text("orchard harvest notes", encoding="utf-8")
    embeddings = RecordingEmbeddings()
    settings = Settings(
        root=home, home=home, chunk_strategy="window", chunk_chars=5000, chunk_overlap=100
    )
    service = MultiSourceRAG(settings, embeddings)
    service.add_local_source("notes", root)
    service.reconcile()
    # Rewrite the index as a pre-prefix (legacy) one: body-only text keyed by the body hash.
    provider, model = cache_identity(embeddings)
    with service.db.connect() as connection:
        connection.execute("DELETE FROM vectors")
        for row in connection.execute("SELECT id,text,body_hash FROM chunks").fetchall():
            body = row["text"].split("\n", 1)[1]
            connection.execute(
                "UPDATE chunks SET text=?,chunk_hash=body_hash WHERE id=?", (body, row["id"])
            )
        connection.execute(
            "INSERT INTO vectors(chunk_hash,provider,model,vector_json) "
            "SELECT DISTINCT chunk_hash,?,?,'[1.0,0.0]' FROM chunks",
            (provider, model),
        )
        connection.commit()
    embeddings.texts.clear()
    service.reconcile(reextract=True)
    assert "one | one.txt\norchard harvest notes" in embeddings.texts
    assert "copy | copy.txt\norchard harvest notes" in embeddings.texts
    with service.db.connect() as connection:
        hashes = connection.execute(
            "SELECT count(DISTINCT chunk_hash) c,count(DISTINCT body_hash) b FROM chunks"
        ).fetchone()
    assert (hashes["c"], hashes["b"]) == (2, 1)
