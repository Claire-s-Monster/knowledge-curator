"""Webhook event models for Knowledge Curator.

Defines the webhook payload structures received from knowledge-bridge.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator


class WebhookEventType(StrEnum):
    """Types of webhook events from knowledge-bridge."""

    LEARNING_STAGED = "learning.staged"
    OUTCOME_REPORTED = "outcome.reported"
    CURATION_REQUESTED = "curation.requested"


class WebhookEvent(BaseModel):
    """Base webhook event structure.

    Accepts both knowledge-bridge format (type/data) and curator format (event_type/payload).
    """

    event_type: WebhookEventType = Field(
        description="Type of webhook event",
        validation_alias=AliasChoices("type", "event_type"),  # Accept both
    )
    payload: dict[str, Any] = Field(
        description="Event-specific payload data",
        validation_alias=AliasChoices("data", "payload"),  # Accept both
    )
    timestamp: datetime = Field(description="Event timestamp")
    webhook_id: str = Field(
        default_factory=lambda: f"wh-{uuid.uuid4().hex[:12]}",
        description="Unique webhook identifier (auto-generated if not provided)",
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, v: Any) -> datetime:
        """Parse timestamp from string if needed."""
        if isinstance(v, str):
            # Handle ISO format strings
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v


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
