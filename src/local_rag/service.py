from typing import Any, Dict, List, Optional

from .config import Settings
from .db import Database
from .embeddings import EmbeddingProvider, configured_provider
from .extract import Extractor
from .indexer import Indexer
from .ocr_runtime import OCRRuntimeManager
from .search import SearchEngine


class LocalRAG:
    def __init__(self, settings: Settings, embeddings: Optional[EmbeddingProvider] = None):
        settings.initialize_layout()
        self.settings = settings
        self.db = Database(settings.database)
        self.db.migrate()
        self.ocr_runtime = OCRRuntimeManager(
            settings.runtime_dir, settings.model_dir / "pp-ocrv6-small"
        )
        self.embeddings = configured_provider(settings) if embeddings is None else embeddings
        self.extractor = Extractor(self.ocr_runtime)
        self.indexer = Indexer(settings, self.db, self.extractor, self.embeddings)
        self.search_engine = SearchEngine(settings, self.db, self.embeddings)

    def scan(
        self,
        target: Optional[str] = None,
        force_index: bool = False,
        reextract: bool = False,
    ) -> Dict[str, Any]:
        path = self.settings.scope(target) if target else None
        return self.indexer.reconcile(path, force_index=force_index, reextract=reextract)

    def search(
        self,
        query: str,
        limit: int = 8,
        scope: Optional[str] = None,
        mode: str = "hybrid",
    ) -> Dict[str, Any]:
        return self.search_engine.search(query, limit, scope, mode)

    def read(self, path: str, start: int = 0, length: int = 12000) -> Dict[str, Any]:
        return self.search_engine.read(path, start, length)

    def status(self) -> Dict[str, Any]:
        return {
            **self.db.stats(),
            "root": str(self.settings.root),
            "home": str(self.settings.home),
            "database": str(self.settings.database),
            "embedding_provider": self.settings.embedding_provider,
            "embedding_model": self.settings.embedding_model,
            "ocr_runtime_installed": self.ocr_runtime.configure(),
        }

    def reviews(self, status: str = "open") -> List[Dict[str, Any]]:
        return self.db.list_reviews(status)

    def resolve_review(self, review_id: int, resolution: str) -> Dict[str, Any]:
        self.db.resolve_review(review_id, resolution)
        return {"id": review_id, "status": "resolved"}

    def correct_review(
        self,
        review_id: int,
        corrected_text: str,
        evidence: List[Dict[str, Any]],
        actor: str,
    ) -> Dict[str, Any]:
        return self.indexer.correct_review(review_id, corrected_text, evidence, actor)

    def metadata(self, path: str) -> Dict[str, Any]:
        return self.db.metadata(path)

    def add_metadata(
        self, path: str, key: str, value: Any, evidence: List[Dict[str, Any]], actor: str
    ) -> Dict[str, Any]:
        return {"id": self.db.add_metadata(path, key, value, evidence, actor)}

    def add_relationship(
        self,
        source: str,
        target: str,
        relation: str,
        evidence: List[Dict[str, Any]],
        actor: str,
    ) -> Dict[str, Any]:
        return {"id": self.db.add_relationship(source, target, relation, evidence, actor)}
