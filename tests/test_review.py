"""Tests for review_staged_entry task."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from knowledge_curator.clients import KnowledgeEntry, SearchResult, StagedEntry
from knowledge_curator.config import Settings
from knowledge_curator.core.models import DecisionType
from knowledge_curator.llm.client import LLMUsage
from knowledge_curator.tasks.review import (
    LLMReviewResponse,
    ReviewContext,
    ReviewPayload,
    _build_decision,
    _prefilter_by_similarity,
    review_staged_entry,
)


@pytest.fixture
def mock_settings() -> Settings:
    """Create test settings."""
    return Settings(
        anthropic_api_key="test-key",
        log_level="DEBUG",
    )


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """Create mock LLM client."""
    client = MagicMock()
    client.complete_json = AsyncMock()
    return client


@pytest.fixture
def mock_uckn_client() -> MagicMock:
    """Create mock UCKN client."""
    client = MagicMock()
    client.search_similar = AsyncMock(return_value=[])
    client.contribute_pattern = AsyncMock(return_value="uckn-123")
    client.get_pattern = AsyncMock(return_value=None)
    client.update_pattern = AsyncMock(return_value=True)
    return client


@pytest.fixture
def mock_bridge_client() -> MagicMock:
    """Create mock knowledge-bridge client."""
    client = MagicMock()
    client.get_staged_entry = AsyncMock()
    client.notify_decision = AsyncMock(return_value=True)
    return client


@pytest.fixture
def review_context(
    mock_settings: Settings,
    mock_llm_client: MagicMock,
    mock_uckn_client: MagicMock,
    mock_bridge_client: MagicMock,
) -> ReviewContext:
    """Create review context with mocks."""
    return ReviewContext(
        settings=mock_settings,
        llm_client=mock_llm_client,
        uckn_client=mock_uckn_client,
        bridge_client=mock_bridge_client,
    )


@pytest.fixture
def sample_staged_entry() -> StagedEntry:
    """Create sample staged entry for testing."""
    return StagedEntry(
        id="staged-123",
        source="session-intelligence",
        source_id="session-abc",
        content={
            "pattern": "pytest fixture scope error",
            "problem": "Fixture cleanup not running",
            "solution": "Use function scope instead of module scope",
            "tags": ["pytest", "fixtures"],
        },
        status="pending",
    )


class TestPrefilterBySimilarity:
    """Tests for _prefilter_by_similarity function."""

    def test_no_similar_entries_returns_none(self, mock_settings: Settings) -> None:
        """No similar entries should return None (needs LLM)."""
        result = _prefilter_by_similarity([], mock_settings)
        assert result is None

    def test_low_similarity_returns_none(self, mock_settings: Settings) -> None:
        """Low similarity entries should return None (needs LLM)."""
        similar = [
            {"id": "uckn-1", "similarity": 0.85, "content": {}},
            {"id": "uckn-2", "similarity": 0.80, "content": {}},
        ]
        result = _prefilter_by_similarity(similar, mock_settings)
        assert result is None

    def test_high_similarity_rejects_duplicate(self, mock_settings: Settings) -> None:
        """High similarity (>0.95) should reject as duplicate."""
        similar = [
            {"id": "uckn-1", "similarity": 0.97, "content": {}},
        ]
        result = _prefilter_by_similarity(similar, mock_settings)

        assert result is not None
        assert result.decision == DecisionType.REJECT
        assert result.confidence == 0.97
        assert "uckn-1" in result.similar_entries
        assert "duplicate" in result.reason.lower()


class TestBuildDecision:
    """Tests for _build_decision function."""

    def test_promote_decision(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Build promote decision from LLM response."""
        llm_response = LLMReviewResponse(
            decision="promote",
            confidence=0.9,
            reason="Novel and high quality",
            quality_score=0.85,
            novelty_score=0.9,
            generalizability_score=0.8,
        )

        decision = _build_decision(
            sample_staged_entry,
            llm_response,
            [],
            review_context,
        )

        assert decision.decision == DecisionType.PROMOTE
        assert decision.confidence == 0.9
        assert decision.reason == "Novel and high quality"

    def test_reject_decision(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Build reject decision from LLM response."""
        llm_response = LLMReviewResponse(
            decision="reject",
            confidence=0.8,
            reason="Too specific, won't help others",
        )

        decision = _build_decision(
            sample_staged_entry,
            llm_response,
            [{"id": "uckn-1"}],
            review_context,
        )

        assert decision.decision == DecisionType.REJECT
        assert decision.confidence == 0.8
        assert "uckn-1" in decision.similar_entries

    def test_merge_decision(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Build merge decision from LLM response."""
        merged_content = {"pattern": "merged pattern", "solution": "combined solution"}
        llm_response = LLMReviewResponse(
            decision="merge",
            confidence=0.85,
            reason="Complements existing entry",
            merge_with="uckn-existing",
            merged_content=merged_content,
        )

        decision = _build_decision(
            sample_staged_entry,
            llm_response,
            [{"id": "uckn-existing"}],
            review_context,
        )

        assert decision.decision == DecisionType.MERGE
        assert decision.merged_content == merged_content

    def test_flag_human_decision(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Build flag_human decision from LLM response."""
        llm_response = LLMReviewResponse(
            decision="flag_human",
            confidence=0.5,
            reason="Requires domain expertise",
        )

        decision = _build_decision(
            sample_staged_entry,
            llm_response,
            [],
            review_context,
        )

        assert decision.decision == DecisionType.FLAG_HUMAN
        assert decision.confidence == 0.5


class TestReviewStagedEntry:
    """Integration tests for review_staged_entry function."""

    async def test_entry_not_found_raises_error(
        self,
        review_context: ReviewContext,
    ) -> None:
        """Should raise error if entry not found."""
        review_context.bridge_client.get_staged_entry.return_value = None

        payload = ReviewPayload(entry_id="nonexistent")

        with pytest.raises(ValueError, match="Entry not found"):
            await review_staged_entry(payload, review_context)

    async def test_duplicate_entry_rejected_without_llm(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Near-duplicate should be rejected without LLM call."""
        # Setup mock to return staged entry
        review_context.bridge_client.get_staged_entry.return_value = sample_staged_entry

        # Setup knowledge store to return high-similarity match
        review_context.uckn_client.search_similar.return_value = [
            SearchResult(
                entry=KnowledgeEntry(
                    id="uckn-duplicate",
                    problem_pattern="Same content",
                    solution="Same solution",
                ),
                similarity_score=0.98,
            ),
        ]

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(payload, review_context)

        # Should reject as duplicate
        assert decision.decision == DecisionType.REJECT
        assert decision.confidence == 0.98

        # LLM should NOT have been called
        review_context.llm_client.complete_json.assert_not_called()

        # Bridge should have been notified
        review_context.bridge_client.notify_decision.assert_called_once()

    async def test_novel_entry_promoted_after_llm_review(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Novel entry should be promoted after LLM review."""
        # Setup mocks
        review_context.bridge_client.get_staged_entry.return_value = sample_staged_entry
        review_context.uckn_client.search_similar.return_value = []

        # LLM says promote
        review_context.llm_client.complete_json.return_value = (
            {
                "decision": "promote",
                "confidence": 0.9,
                "reason": "Novel and high quality",
                "quality_score": 0.85,
                "novelty_score": 0.95,
                "generalizability_score": 0.8,
            },
            LLMUsage(
                input_tokens=500,
                output_tokens=100,
                total_tokens=600,
                cost_usd=0.01,
            ),
        )

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(payload, review_context)

        # Should promote
        assert decision.decision == DecisionType.PROMOTE
        assert decision.confidence == 0.9

        # LLM should have been called
        review_context.llm_client.complete_json.assert_called_once()

        # Should have promoted to UCKN
        review_context.uckn_client.contribute_pattern.assert_called_once()

        # Bridge should have been notified
        review_context.bridge_client.notify_decision.assert_called_once()

    async def test_partial_overlap_triggers_merge(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Partial overlap should trigger merge decision."""
        # Setup mocks
        review_context.bridge_client.get_staged_entry.return_value = sample_staged_entry

        # Similar entry with moderate overlap
        review_context.uckn_client.search_similar.return_value = [
            SearchResult(
                entry=KnowledgeEntry(
                    id="uckn-related",
                    problem_pattern="Related but different",
                    solution="Related solution",
                ),
                similarity_score=0.88,
            ),
        ]

        # Existing entry for merge
        review_context.uckn_client.get_pattern.return_value = {
            "id": "uckn-related",
            "document": "Related content",
            "metadata": {"title": "Related pattern"},
        }

        # LLM says merge
        merged_content = {"pattern": "merged", "solution": "combined"}
        review_context.llm_client.complete_json.return_value = (
            {
                "decision": "merge",
                "confidence": 0.85,
                "reason": "Complements existing entry",
                "merge_with": "uckn-related",
                "merged_content": merged_content,
            },
            LLMUsage(
                input_tokens=600,
                output_tokens=150,
                total_tokens=750,
                cost_usd=0.015,
            ),
        )

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(payload, review_context)

        # Should merge
        assert decision.decision == DecisionType.MERGE
        assert decision.merged_content == merged_content

        # Should have updated existing entry
        review_context.uckn_client.update_pattern.assert_called_once()

    async def test_low_quality_entry_rejected(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Low quality entry should be rejected."""
        # Setup mocks
        review_context.bridge_client.get_staged_entry.return_value = sample_staged_entry
        review_context.uckn_client.search_similar.return_value = []

        # LLM says reject
        review_context.llm_client.complete_json.return_value = (
            {
                "decision": "reject",
                "confidence": 0.8,
                "reason": "Solution is incorrect",
                "quality_score": 0.3,
                "novelty_score": 0.7,
                "generalizability_score": 0.5,
            },
            LLMUsage(
                input_tokens=500,
                output_tokens=100,
                total_tokens=600,
                cost_usd=0.01,
            ),
        )

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(payload, review_context)

        # Should reject
        assert decision.decision == DecisionType.REJECT
        assert "incorrect" in decision.reason.lower()

        # Should NOT have promoted to UCKN
        review_context.uckn_client.contribute_pattern.assert_not_called()
