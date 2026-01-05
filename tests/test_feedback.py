"""Tests for process_feedback task."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from knowledge_curator.clients import KnowledgeEntry
from knowledge_curator.config import Settings
from knowledge_curator.tasks.feedback import (
    FeedbackContext,
    FeedbackPayload,
    _calculate_quality_score,
    _calculate_recency_factor,
    _evaluate_status_change,
    process_feedback,
    process_feedback_batch,
)


@pytest.fixture
def mock_settings() -> Settings:
    """Create test settings."""
    return Settings(
        anthropic_api_key="test-key",
        log_level="DEBUG",
    )


@pytest.fixture
def mock_knowledge_store_client() -> MagicMock:
    """Create mock knowledge-store client."""
    client = MagicMock()
    client.get_entry = AsyncMock()
    client.update_entry = AsyncMock(return_value=True)
    return client


@pytest.fixture
def mock_bridge_client() -> MagicMock:
    """Create mock knowledge-bridge client."""
    client = MagicMock()
    return client


@pytest.fixture
def feedback_context(
    mock_settings: Settings,
    mock_knowledge_store_client: MagicMock,
    mock_bridge_client: MagicMock,
) -> FeedbackContext:
    """Create feedback context with mocks."""
    return FeedbackContext(
        settings=mock_settings,
        knowledge_store_client=mock_knowledge_store_client,
        bridge_client=mock_bridge_client,
    )


@pytest.fixture
def sample_entry() -> KnowledgeEntry:
    """Create sample knowledge entry for testing."""
    return KnowledgeEntry(
        id="ks-123",
        problem_pattern="pytest fixture scope error",
        solution="Use function scope instead of module scope",
        tags=["pytest", "fixtures"],
        pattern_type="bugfix",
        quality_score=0.5,
        times_applied=5,
        success_count=3,
        failure_count=2,
        status="active",
    )


class TestCalculateQualityScore:
    """Tests for _calculate_quality_score function."""

    def test_unused_entry_returns_neutral(self) -> None:
        """Unused entries should return neutral score 0.5."""
        score = _calculate_quality_score(
            times_applied=0,
            success_count=0,
            failure_count=0,
        )
        assert score == 0.5

    def test_all_success_high_score(self) -> None:
        """100% success rate should give high score."""
        score = _calculate_quality_score(
            times_applied=10,
            success_count=10,
            failure_count=0,
            last_applied=datetime.now(UTC),
        )
        # 0.7 * 1.0 + 0.3 * 1.0 = 1.0
        assert score == pytest.approx(1.0, abs=0.01)

    def test_all_failure_low_score(self) -> None:
        """0% success rate should give low score."""
        score = _calculate_quality_score(
            times_applied=10,
            success_count=0,
            failure_count=10,
            last_applied=datetime.now(UTC),
        )
        # 0.7 * 0.0 + 0.3 * 1.0 = 0.3
        assert score == pytest.approx(0.3, abs=0.01)

    def test_mixed_results_moderate_score(self) -> None:
        """50% success rate should give moderate score."""
        score = _calculate_quality_score(
            times_applied=10,
            success_count=5,
            failure_count=5,
            last_applied=datetime.now(UTC),
        )
        # 0.7 * 0.5 + 0.3 * 1.0 = 0.65
        assert score == pytest.approx(0.65, abs=0.01)

    def test_old_entry_lower_score(self) -> None:
        """Entries not applied recently should have lower recency factor."""
        recent_score = _calculate_quality_score(
            times_applied=10,
            success_count=8,
            failure_count=2,
            last_applied=datetime.now(UTC),
        )

        old_score = _calculate_quality_score(
            times_applied=10,
            success_count=8,
            failure_count=2,
            last_applied=datetime.now(UTC) - timedelta(days=60),
        )

        # Same success rate but different recency
        assert old_score < recent_score


class TestCalculateRecencyFactor:
    """Tests for _calculate_recency_factor function."""

    def test_today_returns_one(self) -> None:
        """Applied today should return 1.0."""
        factor = _calculate_recency_factor(datetime.now(UTC))
        assert factor == 1.0

    def test_never_applied_returns_neutral(self) -> None:
        """Never applied should return 0.5."""
        factor = _calculate_recency_factor(None)
        assert factor == 0.5

    def test_thirty_days_approximately_half(self) -> None:
        """After 30 days (half-life), factor should be ~0.5."""
        factor = _calculate_recency_factor(datetime.now(UTC) - timedelta(days=30))
        assert factor == pytest.approx(0.5, abs=0.05)

    def test_sixty_days_approximately_quarter(self) -> None:
        """After 60 days (2 half-lives), factor should be ~0.25."""
        factor = _calculate_recency_factor(datetime.now(UTC) - timedelta(days=60))
        assert factor == pytest.approx(0.25, abs=0.05)

    def test_decay_is_monotonic(self) -> None:
        """Recency factor should decrease with time."""
        factors = [
            _calculate_recency_factor(datetime.now(UTC) - timedelta(days=d))
            for d in [0, 10, 20, 30, 60, 90]
        ]
        # Each factor should be less than or equal to the previous
        for i in range(1, len(factors)):
            assert factors[i] <= factors[i - 1]


class TestEvaluateStatusChange:
    """Tests for _evaluate_status_change function."""

    def test_insufficient_applications_no_change(
        self, mock_settings: Settings
    ) -> None:
        """Should not change status without enough applications."""
        result = _evaluate_status_change(
            times_applied=3,  # Less than min_applications_canonical (5)
            success_count=3,  # 100% success
            current_status="active",
            settings=mock_settings,
        )
        assert result is None

    def test_high_success_promotes_to_canonical(
        self, mock_settings: Settings
    ) -> None:
        """High success rate should promote to canonical."""
        result = _evaluate_status_change(
            times_applied=10,
            success_count=9,  # 90% success > 80% threshold
            current_status="active",
            settings=mock_settings,
        )
        assert result == "canonical"

    def test_already_canonical_no_change(self, mock_settings: Settings) -> None:
        """Already canonical entries should not be changed."""
        result = _evaluate_status_change(
            times_applied=10,
            success_count=9,
            current_status="canonical",
            settings=mock_settings,
        )
        assert result is None

    def test_low_success_archives(self, mock_settings: Settings) -> None:
        """Low success rate should archive."""
        result = _evaluate_status_change(
            times_applied=10,
            success_count=2,  # 20% success < 30% threshold
            current_status="active",
            settings=mock_settings,
        )
        assert result == "archived"

    def test_already_archived_no_change(self, mock_settings: Settings) -> None:
        """Already archived entries should not be changed."""
        result = _evaluate_status_change(
            times_applied=10,
            success_count=2,
            current_status="archived",
            settings=mock_settings,
        )
        assert result is None

    def test_moderate_success_no_change(self, mock_settings: Settings) -> None:
        """Moderate success rate should not change status."""
        result = _evaluate_status_change(
            times_applied=10,
            success_count=5,  # 50% - between thresholds
            current_status="active",
            settings=mock_settings,
        )
        assert result is None


class TestProcessFeedback:
    """Integration tests for process_feedback function."""

    async def test_entry_not_found_raises_error(
        self,
        feedback_context: FeedbackContext,
    ) -> None:
        """Should raise error if entry not found."""
        feedback_context.knowledge_store_client.get_entry.return_value = None

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="nonexistent",
            outcome="success",
        )

        with pytest.raises(ValueError, match="Entry not found"):
            await process_feedback(payload, feedback_context)

    async def test_success_outcome_increments_success_count(
        self,
        sample_entry: KnowledgeEntry,
        feedback_context: FeedbackContext,
    ) -> None:
        """Success outcome should increment success count."""
        feedback_context.knowledge_store_client.get_entry.return_value = sample_entry

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="ks-123",
            outcome="success",
        )

        result = await process_feedback(payload, feedback_context)

        # Verify update was called with incremented counts
        update_call = feedback_context.knowledge_store_client.update_entry.call_args
        updates = update_call[0][1]

        assert updates["times_applied"] == 6  # Was 5
        assert updates["success_count"] == 4  # Was 3
        assert updates["failure_count"] == 2  # Unchanged
        assert result.knowledge_id == "ks-123"

    async def test_failure_outcome_increments_failure_count(
        self,
        sample_entry: KnowledgeEntry,
        feedback_context: FeedbackContext,
    ) -> None:
        """Failure outcome should increment failure count."""
        feedback_context.knowledge_store_client.get_entry.return_value = sample_entry

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="ks-123",
            outcome="failure",
        )

        result = await process_feedback(payload, feedback_context)

        update_call = feedback_context.knowledge_store_client.update_entry.call_args
        updates = update_call[0][1]

        assert updates["times_applied"] == 6
        assert updates["success_count"] == 3  # Unchanged
        assert updates["failure_count"] == 3  # Was 2
        assert result.knowledge_id == "ks-123"

    async def test_partial_outcome_increments_both(
        self,
        sample_entry: KnowledgeEntry,
        feedback_context: FeedbackContext,
    ) -> None:
        """Partial outcome should increment both counts by 0.5.

        Note: Counts are rounded using Python's round() which uses banker's
        rounding (round half to even). So 3.5 rounds to 4, but 2.5 rounds to 2.
        """
        feedback_context.knowledge_store_client.get_entry.return_value = sample_entry

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="ks-123",
            outcome="partial",
        )

        result = await process_feedback(payload, feedback_context)

        update_call = feedback_context.knowledge_store_client.update_entry.call_args
        updates = update_call[0][1]

        assert updates["times_applied"] == 6
        # Partial adds 0.5 to each, result is rounded (banker's rounding)
        assert updates["success_count"] == 4  # round(3.5) = 4 (rounds to even)
        assert updates["failure_count"] == 2  # round(2.5) = 2 (rounds to even)
        assert result.knowledge_id == "ks-123"

    async def test_high_success_triggers_canonical_status(
        self,
        feedback_context: FeedbackContext,
    ) -> None:
        """High success rate should trigger promotion to canonical."""
        # Entry with high success rate after this feedback
        entry = KnowledgeEntry(
            id="ks-high",
            problem_pattern="High success pattern",
            solution="A great solution",
            quality_score=0.7,
            times_applied=9,  # Will become 10
            success_count=8,  # Will become 9 (90%)
            failure_count=1,
            status="active",
        )
        feedback_context.knowledge_store_client.get_entry.return_value = entry

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="ks-high",
            outcome="success",
        )

        result = await process_feedback(payload, feedback_context)

        update_call = feedback_context.knowledge_store_client.update_entry.call_args
        updates = update_call[0][1]

        assert updates.get("status") == "canonical"
        assert result.status_change == "canonical"

    async def test_low_success_triggers_archived_status(
        self,
        feedback_context: FeedbackContext,
    ) -> None:
        """Low success rate should trigger archival."""
        # Entry with low success rate after this feedback
        entry = KnowledgeEntry(
            id="ks-low",
            problem_pattern="Low success pattern",
            solution="A bad solution",
            quality_score=0.3,
            times_applied=9,  # Will become 10
            success_count=2,
            failure_count=7,  # Will become 8 (20% success)
            status="active",
        )
        feedback_context.knowledge_store_client.get_entry.return_value = entry

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="ks-low",
            outcome="failure",
        )

        result = await process_feedback(payload, feedback_context)

        update_call = feedback_context.knowledge_store_client.update_entry.call_args
        updates = update_call[0][1]

        assert updates.get("status") == "archived"
        assert result.status_change == "archived"

    async def test_quality_score_updated(
        self,
        sample_entry: KnowledgeEntry,
        feedback_context: FeedbackContext,
    ) -> None:
        """Quality score should be recalculated and updated."""
        feedback_context.knowledge_store_client.get_entry.return_value = sample_entry

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="ks-123",
            outcome="success",
        )

        result = await process_feedback(payload, feedback_context)

        # New success rate: 4/6 = 0.667, recency ~1.0
        # Expected: 0.7 * 0.667 + 0.3 * 1.0 = 0.767
        assert result.previous_score == 0.5
        assert result.new_quality_score > result.previous_score
        assert 0.6 < result.new_quality_score < 0.9

    async def test_action_taken_description(
        self,
        sample_entry: KnowledgeEntry,
        feedback_context: FeedbackContext,
    ) -> None:
        """Action taken should describe what was done."""
        feedback_context.knowledge_store_client.get_entry.return_value = sample_entry

        payload = FeedbackPayload(
            session_id="session-abc",
            knowledge_id="ks-123",
            outcome="success",
        )

        result = await process_feedback(payload, feedback_context)

        assert "6 applications" in result.action_taken
        assert "Quality score:" in result.action_taken
        assert "->" in result.action_taken


class TestProcessFeedbackBatch:
    """Tests for process_feedback_batch function."""

    async def test_batch_processes_all_items(
        self,
        sample_entry: KnowledgeEntry,
        feedback_context: FeedbackContext,
    ) -> None:
        """Batch of feedback items should all be processed successfully."""
        # Create entries for each feedback
        entry1 = KnowledgeEntry(
            id="ks-001",
            problem_pattern="Pattern 1",
            solution="Solution 1",
            quality_score=0.5,
            times_applied=3,
            success_count=2,
            failure_count=1,
            status="active",
        )
        entry2 = KnowledgeEntry(
            id="ks-002",
            problem_pattern="Pattern 2",
            solution="Solution 2",
            quality_score=0.6,
            times_applied=5,
            success_count=4,
            failure_count=1,
            status="active",
        )
        entry3 = KnowledgeEntry(
            id="ks-003",
            problem_pattern="Pattern 3",
            solution="Solution 3",
            quality_score=0.4,
            times_applied=2,
            success_count=1,
            failure_count=1,
            status="active",
        )

        # Configure mock to return appropriate entry for each ID
        async def get_entry_side_effect(knowledge_id: str) -> KnowledgeEntry | None:
            entries = {"ks-001": entry1, "ks-002": entry2, "ks-003": entry3}
            return entries.get(knowledge_id)

        feedback_context.knowledge_store_client.get_entry.side_effect = (
            get_entry_side_effect
        )

        payloads = [
            FeedbackPayload(
                session_id="session-1", knowledge_id="ks-001", outcome="success"
            ),
            FeedbackPayload(
                session_id="session-2", knowledge_id="ks-002", outcome="failure"
            ),
            FeedbackPayload(
                session_id="session-3", knowledge_id="ks-003", outcome="partial"
            ),
        ]

        results = await process_feedback_batch(payloads, feedback_context)

        assert len(results) == 3
        assert results[0].knowledge_id == "ks-001"
        assert results[1].knowledge_id == "ks-002"
        assert results[2].knowledge_id == "ks-003"
        # Verify no errors in action_taken
        assert not results[0].action_taken.startswith("Error:")
        assert not results[1].action_taken.startswith("Error:")
        assert not results[2].action_taken.startswith("Error:")

    async def test_batch_handles_partial_failure(
        self,
        sample_entry: KnowledgeEntry,
        feedback_context: FeedbackContext,
    ) -> None:
        """Batch should handle one item failing while others succeed."""
        entry1 = KnowledgeEntry(
            id="ks-good-1",
            problem_pattern="Good pattern 1",
            solution="Good solution 1",
            quality_score=0.5,
            times_applied=3,
            success_count=2,
            failure_count=1,
            status="active",
        )
        entry3 = KnowledgeEntry(
            id="ks-good-2",
            problem_pattern="Good pattern 2",
            solution="Good solution 2",
            quality_score=0.6,
            times_applied=4,
            success_count=3,
            failure_count=1,
            status="active",
        )

        # Configure mock to return None for the middle entry (not found)
        async def get_entry_side_effect(knowledge_id: str) -> KnowledgeEntry | None:
            entries = {"ks-good-1": entry1, "ks-good-2": entry3}
            return entries.get(knowledge_id)  # Returns None for "ks-missing"

        feedback_context.knowledge_store_client.get_entry.side_effect = (
            get_entry_side_effect
        )

        payloads = [
            FeedbackPayload(
                session_id="session-1", knowledge_id="ks-good-1", outcome="success"
            ),
            FeedbackPayload(
                session_id="session-2", knowledge_id="ks-missing", outcome="success"
            ),
            FeedbackPayload(
                session_id="session-3", knowledge_id="ks-good-2", outcome="success"
            ),
        ]

        results = await process_feedback_batch(payloads, feedback_context)

        # All 3 items should have results
        assert len(results) == 3

        # First and third should succeed
        assert results[0].knowledge_id == "ks-good-1"
        assert not results[0].action_taken.startswith("Error:")

        # Second should be an error result
        assert results[1].knowledge_id == "ks-missing"
        assert results[1].action_taken.startswith("Error:")
        assert "not found" in results[1].action_taken.lower()
        assert results[1].new_quality_score == 0.0
        assert results[1].previous_score == 0.0
        assert results[1].status_change is None

        # Third should succeed
        assert results[2].knowledge_id == "ks-good-2"
        assert not results[2].action_taken.startswith("Error:")

    async def test_batch_empty_list_returns_empty(
        self,
        feedback_context: FeedbackContext,
    ) -> None:
        """Empty payloads list should return empty results list."""
        results = await process_feedback_batch([], feedback_context)

        assert results == []
        # Verify get_entry was never called
        feedback_context.knowledge_store_client.get_entry.assert_not_called()
