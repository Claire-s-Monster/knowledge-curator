"""Tests for gap analysis task."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from knowledge_curator.core.models import GapCluster
from knowledge_curator.tasks.gaps import (
    GapContext,
    GapPayload,
    LLMGapResponse,
    identify_gaps,
    _extract_failed_queries,
    _get_existing_categories,
    _build_gap_clusters,
    _get_period_string,
)


@pytest.fixture
def mock_settings() -> MagicMock:
    """Create mock settings."""
    return MagicMock()


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """Create mock LLM client."""
    client = MagicMock()
    client.complete_json = AsyncMock(
        return_value=(
            {
                "gap_clusters": [
                    {
                        "theme": "Database connection errors",
                        "queries": ["db timeout", "connection refused"],
                        "frequency": 15,
                        "suggested_entry": {
                            "pattern_type": "error",
                            "title": "Database connection handling",
                        },
                        "priority": "high",
                    }
                ],
                "summary": "Database-related gaps identified",
                "recommendations": ["Add database troubleshooting patterns"],
            },
            MagicMock(total_tokens=200),
        )
    )
    return client


@pytest.fixture
def mock_knowledge_store() -> MagicMock:
    """Create mock knowledge store client."""
    client = MagicMock()
    client.get_stats = AsyncMock(
        return_value={"pattern_types": ["error", "workflow", "configuration"]}
    )
    return client


@pytest.fixture
def mock_bridge_client() -> MagicMock:
    """Create mock bridge client."""
    client = MagicMock()
    client.get_search_logs = AsyncMock(return_value=[])
    return client


@pytest.fixture
def gap_context(
    mock_settings: MagicMock,
    mock_llm_client: MagicMock,
    mock_knowledge_store: MagicMock,
    mock_bridge_client: MagicMock,
) -> GapContext:
    """Create gap context with mocks."""
    return GapContext(
        settings=mock_settings,
        llm_client=mock_llm_client,
        knowledge_store_client=mock_knowledge_store,
        bridge_client=mock_bridge_client,
    )


@pytest.fixture
def sample_search_logs() -> list[dict[str, Any]]:
    """Create sample search logs."""
    return [
        {"query": "database timeout", "results_count": 0, "avg_similarity": 0.0},
        {"query": "connection refused", "results_count": 0, "avg_similarity": 0.0},
        {"query": "database timeout", "results_count": 0, "avg_similarity": 0.0},
        {"query": "pytest fixtures", "results_count": 5, "avg_similarity": 0.85},
        {"query": "async error handling", "results_count": 1, "avg_similarity": 0.4},
    ]


class TestGapPayload:
    """Tests for GapPayload model."""

    def test_default_values(self) -> None:
        """Test default payload values."""
        payload = GapPayload()
        assert payload.triggered_by == "scheduler"
        assert payload.hours_lookback == 168  # 1 week

    def test_custom_values(self) -> None:
        """Test custom payload values."""
        payload = GapPayload(triggered_by="manual", hours_lookback=72)
        assert payload.hours_lookback == 72


class TestLLMGapResponse:
    """Tests for LLMGapResponse model."""

    def test_default_values(self) -> None:
        """Test default values."""
        response = LLMGapResponse(summary="Test summary")
        assert response.gap_clusters == []
        assert response.recommendations == []

    def test_with_clusters(self) -> None:
        """Test with gap clusters."""
        response = LLMGapResponse(
            gap_clusters=[
                {"theme": "Test", "queries": ["q1", "q2"], "frequency": 5}
            ],
            summary="Found gaps",
            recommendations=["Add patterns"],
        )
        assert len(response.gap_clusters) == 1
        assert len(response.recommendations) == 1


class TestIdentifyGaps:
    """Tests for identify_gaps function."""

    @pytest.mark.asyncio
    async def test_no_search_logs_returns_empty_result(
        self,
        gap_context: GapContext,
    ) -> None:
        """Should return empty result when no search logs."""
        result = await identify_gaps(GapPayload(), gap_context)

        assert result.total_searches == 0
        assert result.failed_searches == 0
        assert result.gap_clusters == []
        assert "No search data" in result.recommendations[0]

    @pytest.mark.asyncio
    async def test_no_failed_queries_returns_comprehensive(
        self,
        gap_context: GapContext,
    ) -> None:
        """Should return comprehensive message when no failed queries."""
        gap_context.bridge_client.get_search_logs = AsyncMock(
            return_value=[
                {"query": "good query", "results_count": 5, "avg_similarity": 0.9}
            ]
        )

        result = await identify_gaps(GapPayload(), gap_context)

        assert result.total_searches == 1
        assert result.failed_searches == 0
        assert "comprehensive" in result.recommendations[0].lower()

    @pytest.mark.asyncio
    async def test_identifies_gaps_from_failed_queries(
        self,
        sample_search_logs: list[dict[str, Any]],
        gap_context: GapContext,
    ) -> None:
        """Should identify gaps from failed queries."""
        gap_context.bridge_client.get_search_logs = AsyncMock(
            return_value=sample_search_logs
        )

        result = await identify_gaps(GapPayload(), gap_context)

        assert result.total_searches == 5
        assert result.failed_searches >= 1
        gap_context.llm_client.complete_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_bridge_error(
        self,
        gap_context: GapContext,
    ) -> None:
        """Should handle bridge client errors."""
        gap_context.bridge_client.get_search_logs = AsyncMock(
            side_effect=Exception("Bridge unavailable")
        )

        result = await identify_gaps(GapPayload(), gap_context)

        assert result.total_searches == 0
        assert "Could not fetch" in result.recommendations[0]

    @pytest.mark.asyncio
    async def test_handles_llm_error(
        self,
        sample_search_logs: list[dict[str, Any]],
        gap_context: GapContext,
    ) -> None:
        """Should handle LLM errors."""
        gap_context.bridge_client.get_search_logs = AsyncMock(
            return_value=sample_search_logs
        )
        gap_context.llm_client.complete_json = AsyncMock(
            side_effect=Exception("LLM error")
        )

        result = await identify_gaps(GapPayload(), gap_context)

        assert "LLM analysis failed" in result.recommendations[0]

    @pytest.mark.asyncio
    async def test_custom_lookback_hours(
        self,
        gap_context: GapContext,
    ) -> None:
        """Should use custom lookback hours from payload."""
        payload = GapPayload(hours_lookback=72)

        await identify_gaps(payload, gap_context)

        gap_context.bridge_client.get_search_logs.assert_called_once_with(
            hours=72, limit=1000
        )

    @pytest.mark.asyncio
    async def test_returns_period_string(
        self,
        sample_search_logs: list[dict[str, Any]],
        gap_context: GapContext,
    ) -> None:
        """Should include period string in result."""
        gap_context.bridge_client.get_search_logs = AsyncMock(
            return_value=sample_search_logs
        )

        result = await identify_gaps(GapPayload(), gap_context)

        # Should be ISO week format like "2026-W01"
        assert "-W" in result.period


class TestExtractFailedQueries:
    """Tests for _extract_failed_queries helper."""

    def test_extracts_zero_result_queries(self) -> None:
        """Should extract queries with zero results."""
        logs = [
            {"query": "failed query", "results_count": 0},
            {"query": "good query", "results_count": 5, "avg_similarity": 0.9},
        ]

        result = _extract_failed_queries(logs)

        assert len(result) == 1
        assert result[0]["query"] == "failed query"

    def test_extracts_low_similarity_queries(self) -> None:
        """Should extract queries with low similarity."""
        logs = [
            {"query": "low quality", "results_count": 2, "avg_similarity": 0.3},
        ]

        result = _extract_failed_queries(logs)

        assert len(result) == 1

    def test_aggregates_duplicate_queries(self) -> None:
        """Should aggregate same queries."""
        logs = [
            {"query": "same query", "results_count": 0},
            {"query": "same query", "results_count": 0},
            {"query": "Same Query", "results_count": 0},  # Different case
        ]

        result = _extract_failed_queries(logs)

        # Should be aggregated (case-insensitive)
        assert len(result) == 1
        assert result[0]["count"] == 3

    def test_sorts_by_count_descending(self) -> None:
        """Should sort by count descending."""
        logs = [
            {"query": "rare", "results_count": 0},
            {"query": "common", "results_count": 0},
            {"query": "common", "results_count": 0},
            {"query": "common", "results_count": 0},
        ]

        result = _extract_failed_queries(logs)

        assert result[0]["query"] == "common"
        assert result[0]["count"] == 3

    def test_handles_empty_queries(self) -> None:
        """Should skip empty queries."""
        logs = [
            {"query": "", "results_count": 0},
            {"query": "   ", "results_count": 0},
        ]

        result = _extract_failed_queries(logs)

        assert len(result) == 0

    def test_calculates_avg_results(self) -> None:
        """Should calculate running average of results for failed queries."""
        logs = [
            {"query": "test", "results_count": 0, "avg_similarity": 0.3},  # failed
            {"query": "test", "results_count": 2, "avg_similarity": 0.3},  # failed (low similarity)
        ]

        result = _extract_failed_queries(logs)

        # (0 + 2) / 2 = 1.0
        assert result[0]["avg_results"] == 1.0


class TestGetExistingCategories:
    """Tests for _get_existing_categories helper."""

    @pytest.mark.asyncio
    async def test_returns_categories_from_stats(
        self,
        gap_context: GapContext,
    ) -> None:
        """Should return categories from knowledge store stats."""
        result = await _get_existing_categories(gap_context)

        assert "error" in result
        assert "workflow" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(
        self,
        gap_context: GapContext,
    ) -> None:
        """Should return empty list on error."""
        gap_context.knowledge_store_client.get_stats = AsyncMock(
            side_effect=Exception("Error")
        )

        result = await _get_existing_categories(gap_context)

        assert result == []


class TestBuildGapClusters:
    """Tests for _build_gap_clusters helper."""

    def test_builds_gap_cluster_models(self) -> None:
        """Should build GapCluster models from raw data."""
        raw = [
            {
                "theme": "Database errors",
                "queries": ["db timeout", "connection error"],
                "frequency": 10,
                "suggested_entry": {"pattern_type": "error"},
            }
        ]

        result = _build_gap_clusters(raw)

        assert len(result) == 1
        assert isinstance(result[0], GapCluster)
        assert result[0].representative_query == "Database errors"
        assert result[0].frequency == 10

    def test_uses_first_query_as_theme_fallback(self) -> None:
        """Should use first query as theme if theme missing."""
        raw = [{"queries": ["first query", "second"]}]

        result = _build_gap_clusters(raw)

        assert result[0].representative_query == "first query"

    def test_handles_missing_pattern_type(self) -> None:
        """Should default pattern type to unknown."""
        raw = [{"theme": "Test", "queries": []}]

        result = _build_gap_clusters(raw)

        assert result[0].suggested_pattern_type == "unknown"

    def test_handles_invalid_cluster(self) -> None:
        """Should skip invalid clusters."""
        raw = [None, "invalid", {"theme": "Valid", "queries": []}]

        result = _build_gap_clusters(raw)

        # Should only have the valid one
        assert len(result) == 1


class TestGetPeriodString:
    """Tests for _get_period_string helper."""

    def test_returns_iso_week_format(self) -> None:
        """Should return ISO week format."""
        result = _get_period_string()

        # Should match pattern like "2026-W01"
        assert "-W" in result
        parts = result.split("-W")
        assert len(parts) == 2
        assert parts[0].isdigit()  # Year
        assert parts[1].isdigit()  # Week number
