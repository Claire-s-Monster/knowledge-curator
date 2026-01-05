"""Webhook event models for Knowledge Curator.

Defines the webhook payload structures received from knowledge-bridge.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WebhookEventType(StrEnum):
    """Types of webhook events from knowledge-bridge."""

    LEARNING_STAGED = "learning.staged"
    OUTCOME_REPORTED = "outcome.reported"
    CURATION_REQUESTED = "curation.requested"


class WebhookEvent(BaseModel):
    """Base webhook event structure."""

    event_type: WebhookEventType = Field(description="Type of webhook event")
    payload: dict[str, Any] = Field(description="Event-specific payload data")
    timestamp: datetime = Field(description="Event timestamp")
    webhook_id: str = Field(description="Unique webhook identifier")


class StagedLearningPayload(BaseModel):
    """Payload for learning.staged events."""

    entry_id: str = Field(description="Staged entry identifier")
    content: dict[str, Any] = Field(description="Learning content")
    source: str = Field(
        default="session-intelligence",
        description="Source of the learning",
    )
    session_id: str | None = Field(
        default=None,
        description="Originating session ID",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata",
    )


class OutcomePayload(BaseModel):
    """Payload for outcome.reported events."""

    session_id: str = Field(description="Session that applied the knowledge")
    knowledge_id: str = Field(description="UCKN entry identifier")
    outcome: str = Field(description="success, failure, or partial")
    notes: str = Field(default="", description="Additional outcome notes")
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Application context",
    )


class CurationRequestPayload(BaseModel):
    """Payload for curation.requested events (manual review)."""

    entry_id: str = Field(description="Entry requiring review")
    reason: str = Field(description="Reason for manual review request")
    requester: str = Field(description="Who requested the review")
    priority: str = Field(
        default="normal",
        description="Review priority (low, normal, high, critical)",
    )


class WebhookResponse(BaseModel):
    """Standard webhook response."""

    status: str = Field(description="accepted, rejected, or error")
    task_id: str | None = Field(
        default=None,
        description="Created task ID if accepted",
    )
    error: str | None = Field(
        default=None,
        description="Error message if rejected/error",
    )


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(description="healthy or unhealthy")
    version: str = Field(description="Curator version")
    database: str = Field(description="Database connection status")
    queue: dict[str, Any] = Field(description="Queue statistics")
    uptime_seconds: float = Field(description="Daemon uptime in seconds")
