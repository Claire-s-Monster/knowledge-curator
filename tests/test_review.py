"""Tests for review_staged_entry task."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from knowledge_curator.clients import KnowledgeEntry, SearchResult, StagedEntry
from knowledge_curator.config import Settings
from knowledge_curator.core.models import DecisionType
from knowledge_curator.database.repository import Repository
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
def mock_knowledge_store_client() -> MagicMock:
    """Create mock knowledge-store client."""
    client = MagicMock()
    client.search_similar = AsyncMock(return_value=[])
    client.contribute_pattern = AsyncMock(return_value="ks-123")
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
    mock_knowledge_store_client: MagicMock,
    mock_bridge_client: MagicMock,
) -> ReviewContext:
    """Create review context with mocks."""
    return ReviewContext(
        settings=mock_settings,
        llm_client=mock_llm_client,
        knowledge_store_client=mock_knowledge_store_client,
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
        review_context.knowledge_store_client.search_similar.return_value = [
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
        review_context.knowledge_store_client.search_similar.return_value = []

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
        review_context.knowledge_store_client.contribute_pattern.assert_called_once()

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
        review_context.knowledge_store_client.search_similar.return_value = [
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
        review_context.knowledge_store_client.get_pattern.return_value = {
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
        review_context.knowledge_store_client.update_pattern.assert_called_once()

    async def test_low_quality_entry_rejected(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """Low quality entry should be rejected."""
        # Setup mocks
        review_context.bridge_client.get_staged_entry.return_value = sample_staged_entry
        review_context.knowledge_store_client.search_similar.return_value = []

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
        review_context.knowledge_store_client.contribute_pattern.assert_not_called()


class TestReviewDecisionLogPersistence:
    """Tests for the decision-log audit write on the production review path
    (issue #6). These exercise `review_staged_entry` end-to-end against a
    real `Repository`, not `repository.log_decision` directly, since the
    original bug was that production code never called the writer at all.
    """

    async def test_review_writes_decision_log_row_via_handler(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
        repository: Repository,
    ) -> None:
        """LLM-evaluated decisions must be persisted with real token counts.

        Pre-fix analysis: `_log_decision` did not exist and
        `review_staged_entry` never called `repository.log_decision`, so no
        row would ever be written and `len(rows) == 1` would fail with 0
        rows. Even patched in isolation, the previous `_build_decision` call
        site did not thread `usage` through at all, so `tokens_input` /
        `tokens_output` on the resulting decision were hardcoded to 0 --
        this is the `== 1234` / `== 567` assertions below failing with
        `0 == 1234` and `0 == 567`.
        """
        review_context.repository = repository
        review_context.bridge_client.get_staged_entry.return_value = (
            sample_staged_entry
        )
        review_context.knowledge_store_client.search_similar.return_value = []

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
                input_tokens=1234,
                output_tokens=567,
                total_tokens=1801,
                cost_usd=0.02,
            ),
        )

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(
            payload, review_context, task_id="task-abc"
        )

        cursor = await repository.conn.execute(
            "SELECT task_id, entry_id, decision, confidence, reason, "
            "tokens_input, tokens_output FROM decision_log"
        )
        rows = await cursor.fetchall()

        assert len(rows) == 1
        row = rows[0]
        assert row["task_id"] == "task-abc"
        assert row["entry_id"] == decision.entry_id
        assert row["decision"] == decision.decision.value
        assert row["confidence"] == pytest.approx(decision.confidence)
        assert row["reason"] == decision.reason
        # Critical assertion: pins the original bug (hardcoded zeros).
        assert row["tokens_input"] == 1234
        assert row["tokens_output"] == 567

    async def test_review_decision_log_reflects_billed_input_with_cache_tokens(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
        repository: Repository,
    ) -> None:
        """decision_log.tokens_input must include cache creation/read tokens,
        not just the uncached `input_tokens` figure.

        The Claude Agent SDK's harness caches system prompt and tools
        heavily, so `input_tokens` alone collapses to single digits while
        the real billed input is much larger. This pins that the review
        handler reads `usage.billed_input_tokens`, not `usage.input_tokens`.
        """
        review_context.repository = repository
        review_context.bridge_client.get_staged_entry.return_value = (
            sample_staged_entry
        )
        review_context.knowledge_store_client.search_similar.return_value = []

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
                input_tokens=2,
                output_tokens=259,
                total_tokens=261,
                cost_usd=0.0177,
                cache_creation_input_tokens=500,
                cache_read_input_tokens=1000,
            ),
        )

        payload = ReviewPayload(entry_id="staged-123")
        await review_staged_entry(payload, review_context, task_id="task-cache")

        cursor = await repository.conn.execute(
            "SELECT tokens_input, tokens_output FROM decision_log"
        )
        rows = await cursor.fetchall()

        assert len(rows) == 1
        # 2 (uncached) + 500 (cache creation) + 1000 (cache read) = 1502.
        assert rows[0]["tokens_input"] == 1502
        assert rows[0]["tokens_output"] == 259

    async def test_review_prefilter_path_also_writes_decision_log(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
        repository: Repository,
    ) -> None:
        """The similarity pre-filter early-return must still write an audit
        row, with zero token counts -- which is legitimate here (no LLM call
        happened), unlike the LLM-path bug pinned above.
        """
        review_context.repository = repository
        review_context.bridge_client.get_staged_entry.return_value = (
            sample_staged_entry
        )
        # similarity_duplicate default threshold is 0.95; 0.97 triggers it.
        review_context.knowledge_store_client.search_similar.return_value = [
            SearchResult(
                entry=KnowledgeEntry(
                    id="uckn-duplicate",
                    problem_pattern="Same content",
                    solution="Same solution",
                ),
                similarity_score=0.97,
            ),
        ]

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(
            payload, review_context, task_id="task-prefilter"
        )

        assert decision.decision == DecisionType.REJECT
        review_context.llm_client.complete_json.assert_not_called()

        cursor = await repository.conn.execute(
            "SELECT task_id, tokens_input, tokens_output FROM decision_log"
        )
        rows = await cursor.fetchall()

        assert len(rows) == 1
        assert rows[0]["task_id"] == "task-prefilter"
        # Zero is correct here: the prefilter short-circuits before any LLM
        # call, so there is no usage to record.
        assert rows[0]["tokens_input"] == 0
        assert rows[0]["tokens_output"] == 0

    async def test_review_without_repository_still_succeeds(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
    ) -> None:
        """No repository and no task_id: review must still return a
        decision, pinning the no-op guard so the audit write can never
        become load-bearing for the review outcome itself.
        """
        review_context.repository = None
        review_context.bridge_client.get_staged_entry.return_value = (
            sample_staged_entry
        )
        review_context.knowledge_store_client.search_similar.return_value = []
        review_context.llm_client.complete_json.return_value = (
            {
                "decision": "promote",
                "confidence": 0.9,
                "reason": "Novel and high quality",
            },
            LLMUsage(
                input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001
            ),
        )

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(payload, review_context)

        assert decision.decision == DecisionType.PROMOTE

    async def test_decision_log_failure_does_not_fail_review(
        self,
        sample_staged_entry: StagedEntry,
        review_context: ReviewContext,
        repository: Repository,
    ) -> None:
        """A repository whose log_decision raises must not propagate: the
        curation decision has already succeeded and must not be undone by
        an audit-write failure.
        """
        review_context.repository = repository
        review_context.bridge_client.get_staged_entry.return_value = (
            sample_staged_entry
        )
        review_context.knowledge_store_client.search_similar.return_value = []
        review_context.llm_client.complete_json.return_value = (
            {
                "decision": "promote",
                "confidence": 0.9,
                "reason": "Novel and high quality",
            },
            LLMUsage(
                input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.001
            ),
        )

        async def _raise(*args: object, **kwargs: object) -> None:
            raise RuntimeError("db exploded")

        repository.log_decision = _raise  # type: ignore[method-assign]

        payload = ReviewPayload(entry_id="staged-123")
        decision = await review_staged_entry(
            payload, review_context, task_id="task-fail"
        )

        assert decision.decision == DecisionType.PROMOTE
