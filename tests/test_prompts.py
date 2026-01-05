"""Tests for prompt templates."""

import pytest

from knowledge_curator.llm.prompts import (
    REVIEW_SYSTEM_PROMPT,
    format_dedup_prompt,
    format_gap_analysis_prompt,
    format_obsolescence_prompt,
    format_review_prompt,
)


class TestReviewPrompts:
    """Tests for review prompt formatting."""

    def test_format_review_prompt_basic(self) -> None:
        """Test basic review prompt formatting."""
        prompt = format_review_prompt(
            entry_id="test-123",
            content={
                "pattern": "Test pattern",
                "solution": "Test solution",
            },
            source="session-intelligence",
            session_id="session-456",
            similar_entries=[],
        )

        assert "test-123" in prompt
        assert "Test pattern" in prompt
        assert "Test solution" in prompt
        assert "session-intelligence" in prompt
        assert "No similar entries found" in prompt

    def test_format_review_prompt_with_similar(self) -> None:
        """Test review prompt with similar entries."""
        similar = [
            {
                "id": "similar-1",
                "similarity": 0.92,
                "content": {"pattern": "Similar pattern"},
            },
            {
                "id": "similar-2",
                "similarity": 0.88,
                "content": {"pattern": "Another similar"},
            },
        ]

        prompt = format_review_prompt(
            entry_id="test-123",
            content={"pattern": "Test"},
            source="test",
            session_id=None,
            similar_entries=similar,
        )

        assert "similar-1" in prompt
        assert "similar-2" in prompt
        assert "0.92" in prompt
        assert "Similar pattern" in prompt

    def test_format_review_prompt_limits_similar(self) -> None:
        """Test that review prompt limits similar entries."""
        # Create 10 similar entries
        similar = [
            {"id": f"similar-{i}", "similarity": 0.9, "content": {}}
            for i in range(10)
        ]

        prompt = format_review_prompt(
            entry_id="test",
            content={},
            source="test",
            session_id=None,
            similar_entries=similar,
        )

        # Should only include first 5
        assert "similar-4" in prompt
        assert "similar-5" not in prompt

    def test_review_system_prompt_exists(self) -> None:
        """Test that system prompt is defined."""
        assert len(REVIEW_SYSTEM_PROMPT) > 100
        assert "curator" in REVIEW_SYSTEM_PROMPT.lower()


class TestDedupPrompts:
    """Tests for deduplication prompt formatting."""

    def test_format_dedup_prompt_basic(self) -> None:
        """Test basic dedup prompt formatting."""
        entries = [
            {
                "id": "entry-1",
                "created_at": "2026-01-01",
                "success_rate": 0.8,
                "times_applied": 10,
                "content": {"pattern": "Pattern 1"},
            },
            {
                "id": "entry-2",
                "created_at": "2026-01-02",
                "success_rate": 0.9,
                "times_applied": 5,
                "content": {"pattern": "Pattern 2"},
            },
        ]

        prompt = format_dedup_prompt(entries)

        assert "entry-1" in prompt
        assert "entry-2" in prompt
        assert "80%" in prompt
        assert "90%" in prompt
        assert "Pattern 1" in prompt

    def test_format_dedup_prompt_empty(self) -> None:
        """Test dedup prompt with no entries."""
        prompt = format_dedup_prompt([])
        assert "Duplicate Cluster" in prompt


class TestObsolescencePrompts:
    """Tests for obsolescence prompt formatting."""

    def test_format_obsolescence_prompt(self) -> None:
        """Test obsolescence prompt formatting."""
        prompt = format_obsolescence_prompt(
            entry_id="old-entry-1",
            content={
                "pattern": "Old pattern",
                "solution": "Deprecated solution",
            },
            created_at="2025-01-01",
            last_applied="2025-06-01",
            success_rate=0.3,
            recent_outcomes=["failure", "failure", "success", "failure"],
        )

        assert "old-entry-1" in prompt
        assert "Old pattern" in prompt
        assert "2025-01-01" in prompt
        assert "30%" in prompt
        assert "failure" in prompt

    def test_format_obsolescence_never_applied(self) -> None:
        """Test obsolescence prompt for never-applied entry."""
        prompt = format_obsolescence_prompt(
            entry_id="unused-1",
            content={"pattern": "Unused"},
            created_at="2025-01-01",
            last_applied=None,
            success_rate=0.0,
            recent_outcomes=[],
        )

        assert "Never" in prompt
        assert "None" in prompt


class TestGapAnalysisPrompts:
    """Tests for gap analysis prompt formatting."""

    def test_format_gap_analysis_prompt(self) -> None:
        """Test gap analysis prompt formatting."""
        failed_queries = [
            {"query": "how to fix error X", "count": 15, "avg_results": 0.2},
            {"query": "configure Y setting", "count": 8, "avg_results": 0.5},
        ]

        prompt = format_gap_analysis_prompt(
            failed_queries=failed_queries,
            existing_categories=["error", "configuration", "workflow"],
        )

        assert "how to fix error X" in prompt
        assert "count: 15" in prompt
        assert "error, configuration, workflow" in prompt

    def test_format_gap_analysis_no_categories(self) -> None:
        """Test gap analysis with no existing categories."""
        prompt = format_gap_analysis_prompt(
            failed_queries=[{"query": "test", "count": 1, "avg_results": 0}],
            existing_categories=[],
        )

        assert "None defined" in prompt

    def test_format_gap_analysis_limits_queries(self) -> None:
        """Test that gap analysis limits queries."""
        # Create 100 queries
        queries = [
            {"query": f"query-{i}", "count": 1, "avg_results": 0}
            for i in range(100)
        ]

        prompt = format_gap_analysis_prompt(queries, [])

        # Should only include first 50
        assert "query-49" in prompt
        assert "query-50" not in prompt
