"""Public compatibility namespace for local-rag-mcp."""

from __future__ import annotations

from local_rag import __version__
from local_rag.service import MultiSourceRAG

__all__ = ["MultiSourceRAG", "__version__"]
