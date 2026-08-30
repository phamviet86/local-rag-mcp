from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Sequence
from importlib.util import find_spec
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings


class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> tuple[str, str]: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class LocalEmbeddings:
    def __init__(self, model: str, cache_dir: str):
        self.model_name = model
        self.cache_dir = cache_dir
        self._model: Any | None = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> tuple[str, str]:
        return "local", self.model_name

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        model = self._model
        if model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import SentenceTransformer
                    except ImportError as exc:
                        raise RuntimeError(
                            "install local embeddings with: "
                            "pip install 'local-rag-mcp[local-embeddings]'"
                        ) from exc
                    self._model = SentenceTransformer(self.model_name, cache_folder=self.cache_dir)
                model = self._model
        if model is None:
            raise RuntimeError("local embedding model failed to initialize")
        return cast(
            Sequence[Sequence[float]],
            model.encode(list(texts), normalize_embeddings=True).tolist(),
        )


class OpenAIEmbeddings:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30):
        self.base_url, self.api_key, self.model, self.timeout = (
            base_url.rstrip("/"),
            api_key,
            model,
            timeout,
        )

    @property
    def identity(self) -> tuple[str, str]:
        return "openai", self.model

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not self.api_key:
            raise RuntimeError(
                "LOCAL_RAG_MCP_OPENAI_API_KEY is required for remote embeddings "
                "(legacy LOCAL_RAG_OPENAI_API_KEY is also supported)"
            )
        request = Request(
            f"{self.base_url}/embeddings",
            data=json.dumps({"model": self.model, "input": list(texts)}).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode())
        except (HTTPError, URLError, ValueError) as exc:
            raise RuntimeError(f"embedding request failed: {exc}") from exc
        vectors = [
            item["embedding"] for item in sorted(payload["data"], key=lambda item: item["index"])
        ]
        if len(vectors) != len(texts):
            raise RuntimeError("embedding endpoint returned an unexpected vector count")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or 0 in dimensions:
            raise RuntimeError("embedding endpoint returned inconsistent vector dimensions")
        return [_normalize(vector) for vector in vectors]


class UnavailableEmbeddings:
    def __init__(self, provider: str, model: str, reason: str):
        self._identity = provider, model
        self.reason = reason

    @property
    def identity(self) -> tuple[str, str]:
        return self._identity

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise RuntimeError(self.reason)


def configured_provider(settings: Settings) -> EmbeddingProvider | None:
    if settings.embedding_provider == "none":
        return None
    if not settings.embedding_model:
        return UnavailableEmbeddings(
            settings.embedding_provider,
            "unconfigured",
            "LOCAL_RAG_MCP_EMBEDDING_MODEL is required when embeddings are enabled "
            "(legacy LOCAL_RAG_EMBEDDING_MODEL is also supported)",
        )
    if settings.embedding_provider == "local":
        return LocalEmbeddings(settings.embedding_model, str(settings.cache_dir / "embeddings"))
    return OpenAIEmbeddings(
        settings.openai_base_url, settings.openai_api_key or "", settings.embedding_model
    )


def provider_readiness(
    settings: Settings, provider: EmbeddingProvider | None
) -> tuple[bool, str, str | None]:
    """Report configured inference usability without calling a local model or remote API."""
    if provider is None:
        return False, "vectors are unavailable; FTS search remains available", None
    if isinstance(provider, UnavailableEmbeddings):
        return False, provider.reason, "configure_embedding_model"
    if isinstance(provider, OpenAIEmbeddings):
        if not provider.model.strip():
            return False, "LOCAL_RAG_MCP_EMBEDDING_MODEL is required", "configure_embedding_model"
        if not provider.api_key.strip():
            return False, "LOCAL_RAG_MCP_OPENAI_API_KEY is required", "configure_openai_key"
        return True, "OpenAI-compatible embedding provider is configured (not contacted)", None
    if isinstance(provider, LocalEmbeddings):
        if not provider.model_name.strip():
            return False, "LOCAL_RAG_MCP_EMBEDDING_MODEL is required", "configure_embedding_model"
        if find_spec("sentence_transformers") is None:
            return False, "local embedding dependency is not installed", "install_local_embeddings"
        return True, "local embedding provider and model are configured", None
    if settings.embedding_provider != "none" and not settings.embedding_model:
        return False, "LOCAL_RAG_MCP_EMBEDDING_MODEL is required", "configure_embedding_model"
    return True, "embedding provider is configured", None


def cache_identity(provider: EmbeddingProvider) -> tuple[str, str]:
    """Bind cached vectors to provider, model, endpoint, and declared dimensions."""
    name, model = provider.identity
    payload = {
        "provider": name,
        "model": model,
        "endpoint": getattr(provider, "base_url", "local"),
        "dimensions": getattr(provider, "dimensions", None),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return name, f"{model}#{fingerprint}"


def public_identity(provider: EmbeddingProvider | None) -> dict[str, Any] | None:
    if provider is None:
        return None
    name, model = provider.identity
    _, cache_key = cache_identity(provider)
    return {
        "provider": name,
        "model": model,
        "endpoint": getattr(provider, "base_url", "local"),
        "dimensions": getattr(provider, "dimensions", None),
        "fingerprint": cache_key.rsplit("#", 1)[-1],
    }


def _normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector)) or 1.0
    return [float(value) / norm for value in vector]
