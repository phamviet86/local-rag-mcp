from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".pptx"})
DEFAULT_EXCLUSIONS = frozenset(
    {".git", ".hg", ".svn", ".venv", "__pycache__", "node_modules", "$RECYCLE.BIN"}
)


def default_home() -> Path:
    return (
        Path(
            os.environ.get("LOCAL_RAG_MCP_HOME") or os.environ.get("LOCAL_RAG_HOME", "~/.local-rag")
        )
        .expanduser()
        .resolve()
    )


@dataclass(frozen=True)
class Settings:
    root: Path
    home: Path = field(default_factory=default_home)
    extensions: frozenset[str] = SUPPORTED_EXTENSIONS
    exclusions: frozenset[str] = DEFAULT_EXCLUSIONS
    chunk_chars: int = 1200
    chunk_overlap: int = 150
    reconcile_seconds: float = 600.0
    embedding_provider: str = "none"
    embedding_model: str | None = None
    openai_base_url: str = "https://openrouter.ai/api/v1"
    openai_api_key: str | None = None
    ocr_mode: str = "unconfigured"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())
        object.__setattr__(self, "home", self.home.expanduser().resolve())
        object.__setattr__(self, "extensions", frozenset(_extension(e) for e in self.extensions))
        if self.chunk_chars < 100 or not 0 <= self.chunk_overlap < self.chunk_chars:
            raise ValueError("invalid chunk size or overlap")
        if self.embedding_provider not in {"none", "local", "openai"}:
            raise ValueError("embedding_provider must be none, local, or openai")
        if self.ocr_mode not in {"unconfigured", "full", "no-ocr"}:
            raise ValueError("ocr_mode must be unconfigured, full, or no-ocr")

    @property
    def database(self) -> Path:
        return self.home / "index.sqlite3"

    @property
    def extracted_dir(self) -> Path:
        return self.home / "artifacts" / "extracted"

    @property
    def model_dir(self) -> Path:
        return self.home / "models"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def runtime_dir(self) -> Path:
        return self.home / "runtime"

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    def initialize_layout(self) -> None:
        directories = (
            self.home,
            self.extracted_dir,
            self.model_dir,
            self.cache_dir,
            self.runtime_dir,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                directory.chmod(0o700)

    def save(self) -> None:
        self.initialize_layout()
        payload = {
            "root": str(self.root),
            "extensions": sorted(self.extensions),
            "exclusions": sorted(self.exclusions),
            "chunk_chars": self.chunk_chars,
            "chunk_overlap": self.chunk_overlap,
            "reconcile_seconds": self.reconcile_seconds,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "openai_base_url": self.openai_base_url,
            "ocr_mode": self.ocr_mode,
        }
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)

    @classmethod
    def load(cls, home: Path | None = None) -> Settings:
        data_home = (home or default_home()).expanduser().resolve()
        path = data_home / "config.json"
        if not path.exists():
            raise FileNotFoundError(
                f"not initialized: {path}; run 'local-rag-mcp setup --full' or "
                "'local-rag-mcp setup --no-ocr'"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            root=Path(payload["root"]),
            home=data_home,
            extensions=frozenset(payload.get("extensions", SUPPORTED_EXTENSIONS)),
            exclusions=frozenset(payload.get("exclusions", DEFAULT_EXCLUSIONS)),
            chunk_chars=int(payload.get("chunk_chars", 1200)),
            chunk_overlap=int(payload.get("chunk_overlap", 150)),
            reconcile_seconds=float(payload.get("reconcile_seconds", 600)),
            embedding_provider=os.environ.get("LOCAL_RAG_MCP_EMBEDDING_PROVIDER")
            or os.environ.get(
                "LOCAL_RAG_EMBEDDING_PROVIDER", payload.get("embedding_provider", "none")
            ),
            embedding_model=os.environ.get("LOCAL_RAG_MCP_EMBEDDING_MODEL")
            or os.environ.get("LOCAL_RAG_EMBEDDING_MODEL")
            or payload.get("embedding_model"),
            openai_base_url=os.environ.get("LOCAL_RAG_MCP_OPENAI_BASE_URL")
            or os.environ.get(
                "LOCAL_RAG_OPENAI_BASE_URL",
                payload.get("openai_base_url", "https://openrouter.ai/api/v1"),
            ),
            openai_api_key=os.environ.get("LOCAL_RAG_MCP_OPENAI_API_KEY")
            or os.environ.get("LOCAL_RAG_OPENAI_API_KEY")
            or None,
            ocr_mode=str(payload.get("ocr_mode", "unconfigured")),
        )

    def contains(self, path: Path) -> bool:
        candidate = path.expanduser().resolve()
        return candidate == self.root or self.root in candidate.parents

    def excluded(self, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        if not self.contains(resolved):
            return True
        if resolved == self.home or self.home in resolved.parents:
            return True
        relative = resolved.relative_to(self.root)
        return any(part in self.exclusions for part in relative.parts)

    def accepts(self, path: Path) -> bool:
        return not self.excluded(path) and path.suffix.lower() in self.extensions

    def scope(self, value: str | None) -> Path | None:
        if not value:
            return None
        raw = Path(value)
        candidate = (self.root / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if not self.contains(candidate) or self.excluded(candidate):
            raise ValueError("scope must be an included path below the configured root")
        return candidate


def _extension(value: str) -> str:
    lowered = value.lower()
    return lowered if lowered.startswith(".") else "." + lowered


def parse_exclusions(values: Iterable[str]) -> frozenset[str]:
    return frozenset(DEFAULT_EXCLUSIONS.union(value for value in values if value))
