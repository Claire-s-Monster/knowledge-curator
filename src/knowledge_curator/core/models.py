"""Core domain models for Knowledge Curator.

Defines task types, priorities, statuses, and result models used throughout
the application.
"""

from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskPriority(IntEnum):
    """Task priority levels (lower = higher priority)."""

    CRITICAL = 0  # Manual review requests
    HIGH = 1  # New staged entries
    NORMAL = 2  # Feedback processing
    LOW = 3  # Scheduled batch tasks


class TaskStatus(StrEnum):
    """Task execution status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskType(StrEnum):
    """Types of curation tasks."""

    REVIEW_STAGED = "review_staged_entry"
    PROCESS_FEEDBACK = "process_feedback"
    DEDUPLICATE = "deduplicate"
    DETECT_OBSOLESCENCE = "detect_obsolescence"
    IDENTIFY_GAPS = "identify_gaps"
    MANUAL_REVIEW = "manual_review"


class QueuedTask(BaseModel):
    """A task in the persistent queue."""

    id: str = Field(description="Unique task identifier")
    task_type: TaskType = Field(description="Type of curation task")
    priority: TaskPriority = Field(
        default=TaskPriority.NORMAL,
        description="Task priority level",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Task-specific payload data",
    )
    status: TaskStatus = Field(
        default=TaskStatus.PENDING,
        description="Current task status",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Task creation timestamp",
    )
    started_at: datetime | None = Field(
        default=None,
        description="Task processing start timestamp",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="Task completion timestamp",
    )
    retry_count: int = Field(
        default=0,
        description="Number of retry attempts",
    )
    error_message: str | None = Field(
        default=None,
        description="Error message if task failed",
    )


class DecisionType(StrEnum):
    """Types of curation decisions."""

    PROMOTE = "promote"
    REJECT = "reject"
    MERGE = "merge"
    FLAG_HUMAN = "flag_human"
    UPDATE = "update"
    ARCHIVE = "archive"
    KEEP = "keep"


class ReviewDecision(BaseModel):
    """Result of reviewing a staged entry."""

    entry_id: str = Field(description="Staged entry identifier")
    decision: DecisionType = Field(description="Curation decision")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Decision confidence score",
    )
    reason: str = Field(description="Explanation for the decision")
    similar_entries: list[str] = Field(
        default_factory=list,
        description="IDs of similar UCKN entries",
    )
    merged_content: dict[str, Any] | None = Field(
        default=None,
        description="Merged content if decision is MERGE",
    )
    model_used: str = Field(description="LLM model used for evaluation")
    tokens_input: int = Field(description="Input tokens consumed")
    tokens_output: int = Field(description="Output tokens generated")


class FeedbackResult(BaseModel):
    """Result of processing feedback."""

    knowledge_id: str = Field(description="UCKN entry identifier")
    new_quality_score: float = Field(description="Updated quality score")
    previous_score: float = Field(description="Previous quality score")
    status_change: str | None = Field(
        default=None,
        description="New status if changed (canonical, archive)",
    )
    action_taken: str = Field(description="Description of action taken")


class DeduplicationResult(BaseModel):
    """Result of deduplication batch."""

    clusters_found: int = Field(description="Number of duplicate clusters found")
    entries_superseded: int = Field(description="Number of entries marked superseded")
    entries_kept: int = Field(description="Number of entries kept")
    details: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Cluster resolution details",
    )


class ObsolescenceResult(BaseModel):
    """Result of obsolescence detection."""

    entries_reviewed: int = Field(description="Total entries reviewed")
    entries_archived: int = Field(description="Entries archived")
    entries_updated: int = Field(description="Entries updated")
    entries_kept: int = Field(description="Entries kept unchanged")
    details: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Individual decision details",
    )


class GapCluster(BaseModel):
    """A cluster of similar failed queries indicating a knowledge gap."""

    representative_query: str = Field(description="Most representative query")
    similar_queries: list[str] = Field(
        default_factory=list,
        description="Other similar queries in cluster",
    )
    frequency: int = Field(description="Total occurrences")
    suggested_pattern_type: str = Field(
        description="Suggested pattern type for new entry",
    )


class GapAnalysisResult(BaseModel):
    """Result of gap analysis."""

    period: str = Field(description="Analysis period (e.g., 2026-W01)")
    total_searches: int = Field(description="Total searches analyzed")
    failed_searches: int = Field(description="Searches with no/low results")
    gap_clusters: list[GapCluster] = Field(
        default_factory=list,
        description="Identified knowledge gaps",
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Recommendations for filling gaps",
    )


class CostEntry(BaseModel):
    """Daily cost tracking entry."""

    date: str = Field(description="Date (YYYY-MM-DD)")
    model: str = Field(description="Model name")
    tokens_input: int = Field(description="Total input tokens")
    tokens_output: int = Field(description="Total output tokens")
    estimated_cost_usd: float = Field(description="Estimated cost in USD")
    task_count: int = Field(description="Number of tasks processed")
