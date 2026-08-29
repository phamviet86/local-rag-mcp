import json
import threading
from typing import Optional, Protocol, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings


class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> Tuple[str, str]: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class LocalEmbeddings:
    def __init__(self, model: str, cache_dir: str):
        self.model_name = model
        self.cache_dir = cache_dir
        self._model = None
        self._lock = threading.Lock()

    @property
    def identity(self) -> Tuple[str, str]:
        return "local", self.model_name

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import SentenceTransformer
                    except ImportError as exc:
                        raise RuntimeError(
                            "install local embeddings with: "
                            "pip install 'local-rag[local-embeddings]'"
                        ) from exc
                    self._model = SentenceTransformer(self.model_name, cache_folder=self.cache_dir)
        return self._model.encode(list(texts), normalize_embeddings=True).tolist()


class OpenAIEmbeddings:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30):
        self.base_url, self.api_key, self.model, self.timeout = (
            base_url.rstrip("/"),
            api_key,
            model,
            timeout,
        )

    @property
    def identity(self) -> Tuple[str, str]:
        return "openai", self.model

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not self.api_key:
            raise RuntimeError("LOCAL_RAG_OPENAI_API_KEY is required for remote embeddings")
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
        return [
            item["embedding"] for item in sorted(payload["data"], key=lambda item: item["index"])
        ]


class UnavailableEmbeddings:
    def __init__(self, provider: str, model: str, reason: str):
        self._identity = provider, model
        self.reason = reason

    @property
    def identity(self) -> Tuple[str, str]:
        return self._identity

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise RuntimeError(self.reason)


def configured_provider(settings: Settings) -> Optional[EmbeddingProvider]:
    if settings.embedding_provider == "none":
        return None
    if not settings.embedding_model:
        return UnavailableEmbeddings(
            settings.embedding_provider,
            "unconfigured",
            "LOCAL_RAG_EMBEDDING_MODEL is required when embeddings are enabled",
        )
    if settings.embedding_provider == "local":
        return LocalEmbeddings(settings.embedding_model, str(settings.cache_dir / "embeddings"))
    return OpenAIEmbeddings(
        settings.openai_base_url, settings.openai_api_key or "", settings.embedding_model
    )
