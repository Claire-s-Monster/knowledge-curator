"""Tests for UCKN and Knowledge-bridge clients."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from knowledge_curator.clients import (
    CurationDecision,
    KnowledgeBridgeClient,
    KnowledgeEntry,
    KnowledgeStoreClient,
    SearchResult,
    StagedEntry,
)
from knowledge_curator.config import Settings


class TestKnowledgeStoreClient:
    """Tests for KnowledgeStoreClient."""

    @pytest.fixture
    def client(self) -> KnowledgeStoreClient:
        """Create Knowledge Store client for testing."""
        return KnowledgeStoreClient(base_url="http://localhost:4004")

    @pytest.fixture
    def mock_settings(self) -> Settings:
        """Create test settings."""
        return Settings(
            knowledge_store_url="http://localhost:4004",
            anthropic_api_key="test-key",
        )

    def test_from_settings(self, mock_settings: Settings) -> None:
        """Create client from settings."""
        client = KnowledgeStoreClient.from_settings(mock_settings)
        assert client.base_url == "http://localhost:4004"

    async def test_health_check_success(self, client: KnowledgeStoreClient) -> None:
        """Health check returns True when server responds 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.get.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.health_check()
        assert result is True
        mock_http.get.assert_called_once_with("/health")

    async def test_health_check_failure(self, client: KnowledgeStoreClient) -> None:
        """Health check returns False when server unreachable."""
        mock_http = AsyncMock()
        mock_http.get.side_effect = httpx.ConnectError("Connection refused")
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.health_check()
        assert result is False

    async def test_search_similar_success(self, client: KnowledgeStoreClient) -> None:
        """Search returns parsed pattern results via MCP."""
        # MCP JSON-RPC response format
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": str({
                        "results": [
                            {
                                "entry": {
                                    "id": "pattern-1",
                                    "problem_pattern": "Pattern content",
                                    "solution": "Solution 1",
                                    "tags": ["test"],
                                    "pattern_type": "bugfix",
                                    "quality_score": 0.8,
                                },
                                "similarity_score": 0.85,
                            },
                            {
                                "entry": {
                                    "id": "pattern-2",
                                    "problem_pattern": "Another pattern",
                                    "solution": "Solution 2",
                                    "tags": [],
                                    "pattern_type": "bugfix",
                                    "quality_score": 0.5,
                                },
                                "similarity_score": 0.75,
                            },
                        ],
                        "count": 2,
                    }),
                }]
            }
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        results = await client.search_similar(
            query="test query",
            limit=10,
            min_similarity=0.7,
        )

        assert len(results) == 2
        assert results[0].entry.id == "pattern-1"
        assert results[0].similarity_score == 0.85
        assert results[1].entry.id == "pattern-2"

    async def test_search_similar_empty(self, client: KnowledgeStoreClient) -> None:
        """Search returns empty list when no matches."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": str({"results": [], "count": 0}),
                }]
            }
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        results = await client.search_similar(query="obscure query")
        assert results == []

    async def test_get_pattern_found(self, client: KnowledgeStoreClient) -> None:
        """Get pattern returns pattern data when found via MCP."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": str({
                        "entry": {
                            "id": "pattern-1",
                            "problem_pattern": "Content",
                            "solution": "Solution",
                            "tags": [],
                            "pattern_type": "bugfix",
                            "quality_score": 0.5,
                        }
                    }),
                }]
            }
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.get_pattern("pattern-1")
        assert result is not None
        assert result["id"] == "pattern-1"

    async def test_get_pattern_not_found(self, client: KnowledgeStoreClient) -> None:
        """Get pattern returns None when not found via MCP."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": str({"entry": None}),
                }]
            }
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.get_pattern("nonexistent")
        assert result is None

    async def test_contribute_pattern_success(self, client: KnowledgeStoreClient) -> None:
        """Contribute pattern returns pattern ID on success via MCP."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{
                    "type": "text",
                    "text": str({"entry_id": "new-pattern-123", "success": True}),
                }]
            }
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.contribute_pattern(
            document="Pattern content",
            metadata={"title": "New Pattern"},
        )

        assert result == "new-pattern-123"

    async def test_close(self, client: KnowledgeStoreClient) -> None:
        """Close properly closes HTTP client."""
        mock_http = AsyncMock()
        mock_http.is_closed = False
        client._client = mock_http

        await client.close()
        mock_http.aclose.assert_called_once()


class TestKnowledgeBridgeClient:
    """Tests for KnowledgeBridgeClient."""

    @pytest.fixture
    def client(self) -> KnowledgeBridgeClient:
        """Create knowledge-bridge client for testing."""
        return KnowledgeBridgeClient(base_url="http://localhost:4003")

    @pytest.fixture
    def mock_settings(self) -> Settings:
        """Create test settings."""
        return Settings(
            knowledge_bridge_url="http://localhost:4003",
            anthropic_api_key="test-key",
        )

    def test_from_settings(self, mock_settings: Settings) -> None:
        """Create client from settings."""
        client = KnowledgeBridgeClient.from_settings(mock_settings)
        assert client.base_url == "http://localhost:4003"

    async def test_health_check_success(self, client: KnowledgeBridgeClient) -> None:
        """Health check returns True when server responds 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.get.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.health_check()
        assert result is True

    async def test_get_staged_entries_success(
        self, client: KnowledgeBridgeClient
    ) -> None:
        """Get staged entries returns parsed entries."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "result": [
                {
                    "id": "staged-1",
                    "source": "session-intelligence",
                    "source_id": "session-abc",
                    "content": {"pattern": "test"},
                    "status": "pending",
                },
                {
                    "id": "staged-2",
                    "source": "direct",
                    "content": {"solution": "fix"},
                    "status": "pending",
                },
            ],
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        entries = await client.get_staged_entries(status="pending", limit=50)

        assert len(entries) == 2
        assert entries[0].id == "staged-1"
        assert entries[0].source == "session-intelligence"
        assert entries[1].id == "staged-2"

    async def test_get_staged_entries_error(
        self, client: KnowledgeBridgeClient
    ) -> None:
        """Get staged entries returns empty list on error."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "status": "error",
            "error": "Queue unavailable",
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        entries = await client.get_staged_entries()
        assert entries == []

    async def test_notify_decision_success(
        self, client: KnowledgeBridgeClient
    ) -> None:
        """Notify decision returns True on success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        decision = CurationDecision(
            entry_id="staged-1",
            decision="promote",
            reason="High quality",
            confidence=0.9,
        )

        result = await client.notify_decision(decision)
        assert result is True

    async def test_notify_decision_failure(
        self, client: KnowledgeBridgeClient
    ) -> None:
        """Notify decision returns False on failure."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server error",
            request=MagicMock(),
            response=mock_response,
        )

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        decision = CurationDecision(
            entry_id="staged-1",
            decision="promote",
            reason="High quality",
            confidence=0.9,
        )

        result = await client.notify_decision(decision)
        assert result is False

    async def test_register_webhook_success(
        self, client: KnowledgeBridgeClient
    ) -> None:
        """Register webhook returns webhook ID on success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "status": "success",
            "result": {"id": "wh-123"},
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.register_webhook(
            subscriber="curator",
            events=["learning.staged"],
            endpoint="http://localhost:4005/webhook",
        )

        assert result == "wh-123"

    async def test_unregister_webhook_success(
        self, client: KnowledgeBridgeClient
    ) -> None:
        """Unregister webhook returns True on success."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"status": "success"}

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        mock_http.is_closed = False
        client._client = mock_http

        result = await client.unregister_webhook("wh-123")
        assert result is True
