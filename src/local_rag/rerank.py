"""Optional cross-encoder reranking of search candidates (never required for search)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from importlib.util import find_spec
from typing import Any, Protocol

from .config import Settings


class Reranker(Protocol):
    @property
    def identity(self) -> tuple[str, str]: ...

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]: ...


class LocalReranker:
    def __init__(self, model: str, cache_dir: str):
        self.model_name = model
        self.cache_dir = cache_dir
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> tuple[str, str]:
        return "local", self.model_name

    def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        model = self._model
        if model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import CrossEncoder
                    except ImportError as exc:
                        raise RuntimeError(
                            "install local reranking with: "
                            "pip install 'phamviet-local-rag-mcp[rerank]'"
                        ) from exc
                    self._model = CrossEncoder(
                        self.model_name,
                        cache_folder=self.cache_dir,
                    )
                model = self._model
        if model is None:
            raise RuntimeError("local rerank model failed to initialize")
        scores = model.predict([(query, text) for text in texts])
        return [float(score) for score in scores]


def configured_reranker(settings: Settings) -> Reranker | None:
    if settings.rerank_provider == "none":
        return None
    return LocalReranker(settings.rerank_model, str(settings.cache_dir / "rerank"))


def reranker_status(settings: Settings, reranker: Reranker | None) -> dict[str, Any]:
    """Report reranker readiness without loading a model."""
    if reranker is None:
        return {
            "provider": "none",
            "model": None,
            "available": False,
            "top_n": settings.rerank_top_n,
            "message": "reranking is disabled; hybrid ordering is used",
        }
    provider, model = reranker.identity
    available = True
    message = "reranker is configured"
    if isinstance(reranker, LocalReranker) and find_spec("sentence_transformers") is None:
        available = False
        message = "local rerank dependency is not installed"
    return {
        "provider": provider,
        "model": model,
        "available": available,
        "top_n": settings.rerank_top_n,
        "message": message,
    }
