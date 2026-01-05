"""HTTP clients for external services."""

from knowledge_curator.clients.bridge import (
    CurationDecision,
    KnowledgeBridgeClient,
    StagedEntry,
)
from knowledge_curator.clients.knowledge_store import (
    KnowledgeEntry,
    KnowledgeStoreClient,
    SearchResult,
)

# Backwards compatibility aliases
PatternSearchResult = SearchResult
UCKNClient = KnowledgeStoreClient

__all__ = [
    "CurationDecision",
    "KnowledgeBridgeClient",
    "KnowledgeEntry",
    "KnowledgeStoreClient",
    "PatternSearchResult",  # Deprecated alias
    "SearchResult",
    "StagedEntry",
    "UCKNClient",  # Deprecated alias
]
