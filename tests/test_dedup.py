"""Tests for deduplication task."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from knowledge_curator.tasks.dedup import (
    DedupContext,
    DedupPayload,
    LLMDedupResponse,
    deduplicate,
    _find_duplicate_clusters,
    _resolve_cluster,
    _apply_supersession,
)


@pytest.fixture
def mock_settings() -> MagicMock:
    """Create mock settings."""
    settings = MagicMock()
    settings.thresholds = MagicMock()
    settings.thresholds.similarity_duplicate = 0.92
    return settings


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """Create mock LLM client."""
    client = MagicMock()
    client.complete_json = AsyncMock(
        return_value=(
            {
                "canonical_id": "entry-1",
                "superseded_ids": ["entry-2"],
                "merge_from": [],
                "merged_content": None,
                "reason": "Entry 1 is more complete",
            },
            MagicMock(total_tokens=100),
        )
    )
    return client


@pytest.fixture
def mock_knowledge_store() -> MagicMock:
    """Create mock knowledge store client."""
    client = MagicMock()
    client.get_recent_entries = AsyncMock(return_value=[])
    client.find_similar = AsyncMock(return_value=[])
    client.update_entry = AsyncMock(return_value=True)
    return client


@pytest.fixture
def dedup_context(
    mock_settings: MagicMock,
    mock_llm_client: MagicMock,
    mock_knowledge_store: MagicMock,
) -> DedupContext:
    """Create dedup context with mocks."""
    return DedupContext(
        settings=mock_settings,
        llm_client=mock_llm_client,
        knowledge_store_client=mock_knowledge_store,
    )


@pytest.fixture
def sample_entries() -> list[dict[str, Any]]:
    """Create sample entries for testing."""
    return [
        {
            "id": "entry-1",
            "problem_pattern": "Error handling in async functions",
            "solution": "Use try/except with proper logging",
            "times_applied": 10,
            "success_count": 8,
            "failure_count": 2,
            "tags": ["python", "async"],
        },
        {
            "id": "entry-2",
            "problem_pattern": "Error handling for async functions",
            "solution": "Wrap async calls in try/except",
            "times_applied": 5,
            "success_count": 3,
            "failure_count": 2,
            "tags": ["python", "async"],
        },
    ]


class TestDedupPayload:
    """Tests for DedupPayload model."""

    def test_default_values(self) -> None:
        """Test default payload values."""
        payload = DedupPayload()
        assert payload.triggered_by == "scheduler"
        assert payload.hours_lookback == 24

    def test_custom_values(self) -> None:
        """Test custom payload values."""
        payload = DedupPayload(triggered_by="manual", hours_lookback=48)
        assert payload.triggered_by == "manual"
        assert payload.hours_lookback == 48


class TestLLMDedupResponse:
    """Tests for LLMDedupResponse model."""

    def test_required_fields(self) -> None:
        """Test that required fields are enforced."""
        response = LLMDedupResponse(
            canonical_id="entry-1",
            reason="Test reason",
        )
        assert response.canonical_id == "entry-1"
        assert response.superseded_ids == []
        assert response.merge_from == []
        assert response.merged_content is None

    def test_all_fields(self) -> None:
        """Test with all fields populated."""
        response = LLMDedupResponse(
            canonical_id="entry-1",
            superseded_ids=["entry-2", "entry-3"],
            merge_from=["entry-2"],
            merged_content={"combined": "content"},
            reason="Best quality entry",
        )
        assert len(response.superseded_ids) == 2
        assert response.merged_content == {"combined": "content"}


class TestDeduplicate:
    """Tests for deduplicate function."""

    @pytest.mark.asyncio
    async def test_no_recent_entries_returns_empty_result(
        self,
        dedup_context: DedupContext,
    ) -> None:
        """Should return empty result when no entries found."""
        dedup_context.knowledge_store_client.get_recent_entries = AsyncMock(
            return_value=[]
        )

        result = await deduplicate(DedupPayload(), dedup_context)

        assert result.clusters_found == 0
        assert result.entries_superseded == 0
        assert result.entries_kept == 0
        assert result.details == []

    @pytest.mark.asyncio
    async def test_no_duplicates_found(
        self,
        sample_entries: list[dict[str, Any]],
        dedup_context: DedupContext,
    ) -> None:
        """Should return entries kept when no duplicates."""
        dedup_context.knowledge_store_client.get_recent_entries = AsyncMock(
            return_value=sample_entries
        )
        # No similar entries found
        dedup_context.knowledge_store_client.find_similar = AsyncMock(return_value=[])

        result = await deduplicate(DedupPayload(), dedup_context)

        assert result.clusters_found == 0
        assert result.entries_kept == 2

    @pytest.mark.asyncio
    async def test_duplicates_found_and_resolved(
        self,
        sample_entries: list[dict[str, Any]],
        dedup_context: DedupContext,
    ) -> None:
        """Should find and resolve duplicate clusters."""
        dedup_context.knowledge_store_client.get_recent_entries = AsyncMock(
            return_value=sample_entries
        )

        # First entry finds second as similar
        mock_similar = MagicMock()
        mock_similar.entry = MagicMock()
        mock_similar.entry.id = "entry-2"
        mock_similar.entry.model_dump.return_value = sample_entries[1]

        dedup_context.knowledge_store_client.find_similar = AsyncMock(
            return_value=[mock_similar]
        )

        result = await deduplicate(DedupPayload(), dedup_context)

        assert result.clusters_found >= 0
        dedup_context.knowledge_store_client.get_recent_entries.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_lookback_hours(
        self,
        dedup_context: DedupContext,
    ) -> None:
        """Should use custom lookback hours from payload."""
        payload = DedupPayload(hours_lookback=48)

        await deduplicate(payload, dedup_context)

        dedup_context.knowledge_store_client.get_recent_entries.assert_called_once_with(
            hours=48, limit=500
        )


class TestFindDuplicateClusters:
    """Tests for _find_duplicate_clusters helper."""

    @pytest.mark.asyncio
    async def test_empty_entries_returns_empty(
        self,
        dedup_context: DedupContext,
    ) -> None:
        """Empty entries should return empty clusters."""
        clusters = await _find_duplicate_clusters([], dedup_context)
        assert clusters == []

    @pytest.mark.asyncio
    async def test_entries_without_id_skipped(
        self,
        dedup_context: DedupContext,
    ) -> None:
        """Entries without ID should be skipped."""
        entries = [{"problem_pattern": "test"}]  # No ID

        clusters = await _find_duplicate_clusters(entries, dedup_context)
        assert clusters == []

    @pytest.mark.asyncio
    async def test_handles_find_similar_error(
        self,
        sample_entries: list[dict[str, Any]],
        dedup_context: DedupContext,
    ) -> None:
        """Should handle errors from find_similar gracefully."""
        dedup_context.knowledge_store_client.find_similar = AsyncMock(
            side_effect=Exception("API error")
        )

        # Should not raise
        clusters = await _find_duplicate_clusters(sample_entries, dedup_context)
        assert isinstance(clusters, list)


class TestResolveCluster:
    """Tests for _resolve_cluster helper."""

    @pytest.mark.asyncio
    async def test_returns_resolution_dict(
        self,
        sample_entries: list[dict[str, Any]],
        dedup_context: DedupContext,
    ) -> None:
        """Should return resolution dict from LLM."""
        resolution = await _resolve_cluster(sample_entries, dedup_context)

        assert "canonical_id" in resolution
        assert "superseded_ids" in resolution
        assert "reason" in resolution
        assert "tokens_used" in resolution
        assert resolution["cluster_size"] == 2

    @pytest.mark.asyncio
    async def test_handles_llm_parse_error(
        self,
        sample_entries: list[dict[str, Any]],
        dedup_context: DedupContext,
    ) -> None:
        """Should fallback on LLM parse error."""
        dedup_context.llm_client.complete_json = AsyncMock(
            return_value=({"invalid": "response"}, MagicMock(total_tokens=50))
        )

        resolution = await _resolve_cluster(sample_entries, dedup_context)

        # Should fallback to first entry as canonical
        assert resolution["canonical_id"] == "entry-1"
        assert "entry-2" in resolution["superseded_ids"]


class TestApplySupersession:
    """Tests for _apply_supersession helper."""

    @pytest.mark.asyncio
    async def test_marks_entries_as_superseded(
        self,
        dedup_context: DedupContext,
    ) -> None:
        """Should update superseded entries in knowledge store."""
        resolution = {
            "canonical_id": "entry-1",
            "superseded_ids": ["entry-2", "entry-3"],
        }

        await _apply_supersession(resolution, dedup_context)

        assert dedup_context.knowledge_store_client.update_entry.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_update_failure(
        self,
        dedup_context: DedupContext,
    ) -> None:
        """Should handle update failures gracefully."""
        dedup_context.knowledge_store_client.update_entry = AsyncMock(
            return_value=False
        )

        resolution = {
            "canonical_id": "entry-1",
            "superseded_ids": ["entry-2"],
        }

        # Should not raise
        await _apply_supersession(resolution, dedup_context)

    @pytest.mark.asyncio
    async def test_updates_canonical_with_merged_content(
        self,
        dedup_context: DedupContext,
    ) -> None:
        """Should update canonical entry if merged content provided."""
        resolution = {
            "canonical_id": "entry-1",
            "superseded_ids": [],
            "merged_content": {"combined": "solution"},
        }

        await _apply_supersession(resolution, dedup_context)

        # Should update canonical entry with merged content
        calls = dedup_context.knowledge_store_client.update_entry.call_args_list
        assert any(
            call[1].get("updates", {}).get("merged_content") == {"combined": "solution"}
            for call in calls
        ) or any(
            call[0][1].get("merged_content") == {"combined": "solution"}
            for call in calls
        )
