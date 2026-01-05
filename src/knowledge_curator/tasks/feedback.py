"""Feedback processing task for Knowledge Curator.

Implements FR-2.2 from PRD: process_feedback task that:
1. Fetches current entry statistics from knowledge-store
2. Updates success/failure counts based on outcome
3. Recalculates quality score
4. Evaluates for status changes (canonical/archive)
5. Updates knowledge-store entry
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from knowledge_curator.clients import KnowledgeBridgeClient, KnowledgeStoreClient
from knowledge_curator.config import Settings
from knowledge_curator.core.models import FeedbackResult

logger = logging.getLogger(__name__)


class FeedbackPayload(BaseModel):
    """Payload for process_feedback task."""

    session_id: str = Field(description="Session that applied the knowledge")
    knowledge_id: str = Field(description="Knowledge-store entry ID")
    outcome: Literal["success", "failure", "partial"] = Field(
        description="Outcome of applying the knowledge"
    )
    notes: str = Field(default="", description="Additional feedback notes")
    context: dict[str, object] = Field(
        default_factory=dict,
        description="Application context",
    )


@dataclass
class FeedbackContext:
    """Dependencies for feedback task execution."""

    settings: Settings
    knowledge_store_client: KnowledgeStoreClient
    bridge_client: KnowledgeBridgeClient


async def process_feedback(
    payload: FeedbackPayload,
    context: FeedbackContext,
) -> FeedbackResult:
    """Process feedback for a knowledge entry.

    Updates the entry's statistics and quality score based on outcome,
    and evaluates whether the entry's status should change.

    Args:
        payload: Task payload with outcome information.
        context: Execution context with clients.

    Returns:
        FeedbackResult with updated scores and any status change.

    Raises:
        ValueError: If entry not found in knowledge-store.
    """
    knowledge_id = payload.knowledge_id
    logger.info(
        f"Processing feedback for {knowledge_id}: outcome={payload.outcome}, "
        f"session={payload.session_id}"
    )

    # Step 1: Fetch current entry from knowledge-store
    entry = await context.knowledge_store_client.get_entry(knowledge_id)
    if entry is None:
        raise ValueError(f"Entry not found in knowledge-store: {knowledge_id}")

    # Store previous score for comparison
    previous_score = entry.quality_score

    # Step 2: Calculate updated counts based on outcome
    new_times_applied = entry.times_applied + 1
    new_success_count: float = float(entry.success_count)
    new_failure_count: float = float(entry.failure_count)

    if payload.outcome == "success":
        new_success_count += 1.0
    elif payload.outcome == "failure":
        new_failure_count += 1.0
    else:  # partial
        # Partial counts as 0.5 success and 0.5 failure
        new_success_count += 0.5
        new_failure_count += 0.5

    # Step 3: Recalculate quality score
    new_quality_score = _calculate_quality_score(
        times_applied=new_times_applied,
        success_count=new_success_count,
        failure_count=new_failure_count,
        last_applied=datetime.now(UTC),
    )

    # Step 4: Evaluate status change
    status_change = _evaluate_status_change(
        times_applied=new_times_applied,
        success_count=new_success_count,
        current_status=entry.status,
        settings=context.settings,
    )

    # Step 5: Build updates for knowledge-store
    updates: dict[str, object] = {
        "times_applied": new_times_applied,
        "success_count": round(new_success_count),
        "failure_count": round(new_failure_count),
        "quality_score": new_quality_score,
        "last_applied": datetime.now(UTC).isoformat(),
    }

    if status_change:
        updates["status"] = status_change

    # Step 6: Apply updates to knowledge-store
    success = await context.knowledge_store_client.update_entry(knowledge_id, updates)
    if not success:
        raise RuntimeError(
            f"Failed to update entry {knowledge_id} in knowledge-store: feedback not applied"
        )

    # Build action description
    action_parts = [f"Updated statistics: {new_times_applied} applications"]
    action_parts.append(f"(success: {new_success_count}, failure: {new_failure_count})")
    action_parts.append(
        f"Quality score: {previous_score:.2f} -> {new_quality_score:.2f}"
    )
    if status_change:
        action_parts.append(f"Status changed to: {status_change}")

    action_taken = " ".join(action_parts)

    logger.info(
        f"Feedback processed for {knowledge_id}: "
        f"score {previous_score:.2f} -> {new_quality_score:.2f}"
        + (f", status -> {status_change}" if status_change else "")
    )

    return FeedbackResult(
        knowledge_id=knowledge_id,
        new_quality_score=new_quality_score,
        previous_score=previous_score,
        status_change=status_change,
        action_taken=action_taken,
    )


def _calculate_quality_score(
    times_applied: int,
    success_count: float,
    failure_count: float,
    last_applied: datetime | None = None,
) -> float:
    """Calculate weighted quality score.

    Formula from PRD:
    - success_rate = success_count / times_applied
    - recency_factor = decay_by_age(last_applied)
    - quality_score = (0.7 * success_rate) + (0.3 * recency_factor)

    Args:
        times_applied: Total times the pattern was applied.
        success_count: Number of successful applications.
        failure_count: Number of failed applications.
        last_applied: When the pattern was last applied.

    Returns:
        Quality score between 0.0 and 1.0.
    """
    if times_applied == 0:
        return 0.5  # Neutral for unused entries

    # Calculate success rate
    success_rate = success_count / times_applied

    # Calculate recency factor (decays over time)
    recency_factor = _calculate_recency_factor(last_applied)

    # Weighted combination
    quality_score = (0.7 * success_rate) + (0.3 * recency_factor)

    # Clamp to valid range
    return max(0.0, min(1.0, quality_score))


def _calculate_recency_factor(last_applied: datetime | None) -> float:
    """Calculate recency decay factor.

    Uses exponential decay based on days since last application.
    Half-life is approximately 30 days.

    Args:
        last_applied: When the pattern was last applied.

    Returns:
        Recency factor between 0.0 and 1.0.
    """
    if last_applied is None:
        return 0.5  # Neutral if never applied

    now = datetime.now(UTC)

    # Ensure last_applied is timezone-aware
    if last_applied.tzinfo is None:
        last_applied = last_applied.replace(tzinfo=UTC)

    days_since = (now - last_applied).days

    if days_since <= 0:
        return 1.0  # Applied today

    # Exponential decay with 30-day half-life
    # After 30 days: ~0.5, after 60 days: ~0.25, after 90 days: ~0.125
    half_life_days = 30
    decay_rate = math.log(2) / half_life_days
    recency_factor = math.exp(-decay_rate * days_since)

    return recency_factor


def _evaluate_status_change(
    times_applied: int,
    success_count: float,
    current_status: str,
    settings: Settings,
) -> str | None:
    """Evaluate if entry status should change.

    From PRD:
    - If times_applied >= 5 and success_rate >= 0.8 → "canonical" (proven pattern)
    - If times_applied >= 5 and success_rate <= 0.3 → "archive" (failed pattern)

    Args:
        times_applied: Total applications.
        success_count: Successful applications.
        current_status: Current entry status.
        settings: Application settings with thresholds.

    Returns:
        New status string if change needed, None otherwise.
    """
    # Need minimum applications before status changes
    min_applications = settings.thresholds.min_applications_canonical
    if times_applied < min_applications:
        return None

    success_rate = success_count / times_applied if times_applied > 0 else 0

    # Check for promotion to canonical
    if (
        success_rate >= settings.thresholds.success_rate_canonical
        and current_status != "canonical"
    ):
        return "canonical"

    # Check for archival due to low success
    if (
        success_rate <= settings.thresholds.success_rate_archive
        and current_status not in ("archived", "superseded")
    ):
        return "archived"

    return None


async def process_feedback_batch(
    payloads: list[FeedbackPayload],
    context: FeedbackContext,
) -> list[FeedbackResult]:
    """Process multiple feedback items in batch.

    Useful for bulk processing of accumulated feedback.

    Args:
        payloads: List of feedback payloads to process.
        context: Execution context.

    Returns:
        List of FeedbackResults.
    """
    results = []
    for payload in payloads:
        try:
            result = await process_feedback(payload, context)
            results.append(result)
        except Exception as e:
            logger.error(f"Failed to process feedback for {payload.knowledge_id}: {e}")
            # Create error result
            results.append(
                FeedbackResult(
                    knowledge_id=payload.knowledge_id,
                    new_quality_score=0.0,
                    previous_score=0.0,
                    status_change=None,
                    action_taken=f"Error: {e}",
                )
            )

    return results
