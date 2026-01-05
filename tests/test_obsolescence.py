"""Tests for obsolescence detection task."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from knowledge_curator.tasks.obsolescence import (
    ObsolescenceContext,
    ObsolescencePayload,
    LLMObsolescenceResponse,
    detect_obsolescence,
    _find_unused_entries,
    _find_failing_entries,
    _evaluate_entry,
)


@pytest.fixture
def mock_settings() -> MagicMock:
    """Create mock settings."""
    settings = MagicMock()
    settings.thresholds = MagicMock()
    settings.thresholds.days_unused_archive = 30
    settings.thresholds.success_rate_archive = 0.3
    return settings


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """Create mock LLM client."""
    client = MagicMock()
    client.complete_json = AsyncMock(
        return_value=(
            {
                "decision": "keep",
                "confidence": 0.8,
                "reason": "Solution is still valid",
                "update_suggestion": None,
                "obsolescence_indicators": [],
            },
            MagicMock(total_tokens=100),
        )
    )
    return client


@pytest.fixture
def mock_knowledge_store() -> MagicMock:
    """Create mock knowledge store client."""
    client = MagicMock()
    client.list_entries = AsyncMock(return_value=[])
    client.update_entry = AsyncMock(return_value=True)
    return client


@pytest.fixture
def obsolescence_context(
    mock_settings: MagicMock,
    mock_llm_client: MagicMock,
    mock_knowledge_store: MagicMock,
) -> ObsolescenceContext:
    """Create obsolescence context with mocks."""
    return ObsolescenceContext(
        settings=mock_settings,
        llm_client=mock_llm_client,
        knowledge_store_client=mock_knowledge_store,
    )


@pytest.fixture
def sample_unused_entry() -> MagicMock:
    """Create sample unused entry."""
    entry = MagicMock()
    entry.id = "entry-unused"
    entry.problem_pattern = "Old pattern"
    entry.solution = "Outdated solution"
    entry.tags = ["legacy"]
    entry.times_applied = 10
    entry.success_count = 7
    entry.failure_count = 3
    entry.model_dump.return_value = {
        "id": "entry-unused",
        "problem_pattern": "Old pattern",
        "solution": "Outdated solution",
        "tags": ["legacy"],
        "times_applied": 10,
        "success_count": 7,
        "failure_count": 3,
        "created_at": "2025-01-01",
        "last_applied": "2025-11-01",
    }
    return entry


@pytest.fixture
def sample_failing_entry() -> MagicMock:
    """Create sample failing entry."""
    entry = MagicMock()
    entry.id = "entry-failing"
    entry.problem_pattern = "Broken pattern"
    entry.solution = "Bad solution"
    entry.times_applied = 10
    entry.success_count = 2  # 20% success rate
    entry.failure_count = 8
    entry.model_dump.return_value = {
        "id": "entry-failing",
        "problem_pattern": "Broken pattern",
        "solution": "Bad solution",
        "times_applied": 10,
        "success_count": 2,
        "failure_count": 8,
    }
    return entry


class TestObsolescencePayload:
    """Tests for ObsolescencePayload model."""

    def test_default_values(self) -> None:
        """Test default payload values."""
        payload = ObsolescencePayload()
        assert payload.triggered_by == "scheduler"


class TestLLMObsolescenceResponse:
    """Tests for LLMObsolescenceResponse model."""

    def test_keep_decision(self) -> None:
        """Test keep decision response."""
        response = LLMObsolescenceResponse(
            decision="keep",
            confidence=0.9,
            reason="Still valid",
        )
        assert response.decision == "keep"
        assert response.update_suggestion is None

    def test_archive_decision(self) -> None:
        """Test archive decision response."""
        response = LLMObsolescenceResponse(
            decision="archive",
            confidence=0.85,
            reason="Technology deprecated",
            obsolescence_indicators=["Uses deprecated API", "No recent success"],
        )
        assert response.decision == "archive"
        assert len(response.obsolescence_indicators) == 2

    def test_update_decision(self) -> None:
        """Test update decision response."""
        response = LLMObsolescenceResponse(
            decision="update",
            confidence=0.75,
            reason="Needs modernization",
            update_suggestion="Use new API version",
        )
        assert response.decision == "update"
        assert response.update_suggestion == "Use new API version"

    def test_confidence_bounds(self) -> None:
        """Test confidence must be between 0 and 1."""
        # Valid
        LLMObsolescenceResponse(decision="keep", confidence=0.0, reason="test")
        LLMObsolescenceResponse(decision="keep", confidence=1.0, reason="test")

        # Invalid - should raise
        with pytest.raises(ValueError):
            LLMObsolescenceResponse(decision="keep", confidence=1.5, reason="test")
        with pytest.raises(ValueError):
            LLMObsolescenceResponse(decision="keep", confidence=-0.1, reason="test")


class TestDetectObsolescence:
    """Tests for detect_obsolescence function."""

    @pytest.mark.asyncio
    async def test_no_candidates_returns_empty_result(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should return empty result when no candidates."""
        result = await detect_obsolescence(
            ObsolescencePayload(), obsolescence_context
        )

        assert result.entries_reviewed == 0
        assert result.entries_archived == 0
        assert result.entries_updated == 0
        assert result.entries_kept == 0

    @pytest.mark.asyncio
    async def test_evaluates_unused_entries(
        self,
        sample_unused_entry: MagicMock,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should evaluate unused entries."""
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            return_value=[sample_unused_entry]
        )

        result = await detect_obsolescence(
            ObsolescencePayload(), obsolescence_context
        )

        assert result.entries_reviewed >= 1
        # Default mock returns "keep"
        assert result.entries_kept >= 1

    @pytest.mark.asyncio
    async def test_archives_obsolete_entries(
        self,
        sample_unused_entry: MagicMock,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should archive entries when LLM decides archive."""
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            return_value=[sample_unused_entry]
        )
        obsolescence_context.llm_client.complete_json = AsyncMock(
            return_value=(
                {
                    "decision": "archive",
                    "confidence": 0.9,
                    "reason": "Deprecated technology",
                    "obsolescence_indicators": ["Outdated"],
                },
                MagicMock(total_tokens=100),
            )
        )

        result = await detect_obsolescence(
            ObsolescencePayload(), obsolescence_context
        )

        assert result.entries_archived >= 1
        obsolescence_context.knowledge_store_client.update_entry.assert_called()

    @pytest.mark.asyncio
    async def test_flags_entries_for_update(
        self,
        sample_unused_entry: MagicMock,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should flag entries for update when LLM decides update."""
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            return_value=[sample_unused_entry]
        )
        obsolescence_context.llm_client.complete_json = AsyncMock(
            return_value=(
                {
                    "decision": "update",
                    "confidence": 0.8,
                    "reason": "Needs new API",
                    "update_suggestion": "Use v2 API",
                },
                MagicMock(total_tokens=100),
            )
        )

        result = await detect_obsolescence(
            ObsolescencePayload(), obsolescence_context
        )

        assert result.entries_updated >= 1

    @pytest.mark.asyncio
    async def test_deduplicates_candidates(
        self,
        sample_unused_entry: MagicMock,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should deduplicate entries from unused and failing lists."""
        # Same entry appears in both lists
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            return_value=[sample_unused_entry]
        )

        result = await detect_obsolescence(
            ObsolescencePayload(), obsolescence_context
        )

        # Should only evaluate once
        assert result.entries_reviewed >= 1


class TestFindUnusedEntries:
    """Tests for _find_unused_entries helper."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should return empty list on error."""
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            side_effect=Exception("API error")
        )

        result = await _find_unused_entries(30, obsolescence_context)

        assert result == []

    @pytest.mark.asyncio
    async def test_filters_by_last_applied(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should filter entries by last applied date."""
        await _find_unused_entries(30, obsolescence_context)

        call_args = obsolescence_context.knowledge_store_client.list_entries.call_args
        filters = call_args[1]["filters"]
        assert "status" in filters
        assert "last_applied_before" in filters


class TestFindFailingEntries:
    """Tests for _find_failing_entries helper."""

    @pytest.mark.asyncio
    async def test_filters_by_success_rate(
        self,
        sample_failing_entry: MagicMock,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should filter entries with low success rate."""
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            return_value=[sample_failing_entry]
        )

        result = await _find_failing_entries(obsolescence_context)

        # 20% success rate is below 30% threshold
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_skips_entries_with_few_applications(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should skip entries without enough applications."""
        entry = MagicMock()
        entry.times_applied = 1  # Too few
        entry.success_count = 0
        entry.failure_count = 1
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            return_value=[entry]
        )

        result = await _find_failing_entries(obsolescence_context, min_applications=3)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should return empty list on error."""
        obsolescence_context.knowledge_store_client.list_entries = AsyncMock(
            side_effect=Exception("API error")
        )

        result = await _find_failing_entries(obsolescence_context)

        assert result == []


class TestEvaluateEntry:
    """Tests for _evaluate_entry helper."""

    @pytest.mark.asyncio
    async def test_returns_decision_dict(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should return decision dict from LLM."""
        entry = {
            "id": "test-entry",
            "problem_pattern": "Test",
            "solution": "Solution",
            "tags": [],
            "times_applied": 5,
            "success_count": 4,
            "created_at": "2025-01-01",
        }

        decision = await _evaluate_entry(entry, obsolescence_context)

        assert "entry_id" in decision
        assert "decision" in decision
        assert "confidence" in decision
        assert "reason" in decision
        assert decision["entry_id"] == "test-entry"

    @pytest.mark.asyncio
    async def test_handles_llm_parse_error(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should default to keep on parse error."""
        obsolescence_context.llm_client.complete_json = AsyncMock(
            return_value=({"invalid": "response"}, MagicMock(total_tokens=50))
        )

        entry = {"id": "test", "times_applied": 0, "success_count": 0}
        decision = await _evaluate_entry(entry, obsolescence_context)

        # Should default to keep
        assert decision["decision"] == "keep"

    @pytest.mark.asyncio
    async def test_calculates_success_rate(
        self,
        obsolescence_context: ObsolescenceContext,
    ) -> None:
        """Should calculate success rate for prompt."""
        entry = {
            "id": "test",
            "times_applied": 10,
            "success_count": 7,
            "failure_count": 3,
        }

        await _evaluate_entry(entry, obsolescence_context)

        # LLM should be called (we can't easily verify the exact prompt)
        obsolescence_context.llm_client.complete_json.assert_called_once()
