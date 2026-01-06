"""Obsolescence detection task for Knowledge Curator.

Implements FR-2.4: Daily obsolescence detection at 3 AM.
Finds outdated entries that should be archived or updated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from knowledge_curator.clients import KnowledgeStoreClient
from knowledge_curator.config import Settings
from knowledge_curator.core.models import ObsolescenceResult
from knowledge_curator.llm.client import CuratorLLMClient
from knowledge_curator.llm.prompts import (
    OBSOLESCENCE_SYSTEM_PROMPT,
    format_obsolescence_prompt,
)

logger = logging.getLogger(__name__)


class ObsolescencePayload(BaseModel):
    """Payload for detect_obsolescence task."""

    triggered_by: str = Field(default="scheduler", description="Task trigger source")


@dataclass
class ObsolescenceContext:
    """Dependencies for obsolescence detection task."""

    settings: Settings
    llm_client: CuratorLLMClient
    knowledge_store_client: KnowledgeStoreClient


class LLMObsolescenceResponse(BaseModel):
    """Parsed response from LLM obsolescence assessment."""

    decision: Literal["keep", "update", "archive"] = Field(description="Action to take")
    confidence: float = Field(ge=0.0, le=1.0, description="Decision confidence")
    reason: str = Field(description="Explanation of the assessment")
    update_suggestion: str | None = Field(
        default=None, description="Suggested update if decision is 'update'"
    )
    obsolescence_indicators: list[str] = Field(
        default_factory=list, description="Specific indicators of obsolescence"
    )


async def detect_obsolescence(
    payload: ObsolescencePayload,
    context: ObsolescenceContext,
) -> ObsolescenceResult:
    """Run obsolescence detection batch.

    Workflow:
    1. Find entries not applied in 30+ days
    2. Find entries with repeated failures (low success rate)
    3. LLM evaluates: keep, update, or archive
    4. Apply status changes

    Args:
        payload: Task configuration.
        context: Execution context with clients.

    Returns:
        ObsolescenceResult with individual decisions.
    """
    logger.info("Starting obsolescence detection")

    # Step 1: Find candidates - unused entries
    days_threshold = context.settings.thresholds.days_unused_archive
    unused_entries = await _find_unused_entries(days_threshold, context)
    logger.debug(f"Found {len(unused_entries)} unused entries")

    # Step 2: Find candidates - entries with repeated failures
    failing_entries = await _find_failing_entries(context)
    logger.debug(f"Found {len(failing_entries)} failing entries")

    # Combine and deduplicate candidates
    candidate_ids = {e["id"] for e in unused_entries}
    candidates = list(unused_entries)
    for entry in failing_entries:
        if entry["id"] not in candidate_ids:
            candidates.append(entry)
            candidate_ids.add(entry["id"])

    if not candidates:
        logger.info("No obsolescence candidates found")
        return ObsolescenceResult(
            entries_reviewed=0,
            entries_archived=0,
            entries_updated=0,
            entries_kept=0,
            details=[],
        )

    logger.info(f"Total obsolescence candidates: {len(candidates)}")

    # Step 3: Evaluate each candidate with LLM
    details: list[dict[str, Any]] = []
    archived = 0
    updated = 0
    kept = 0

    for entry in candidates:
        try:
            decision = await _evaluate_entry(entry, context)
            details.append(decision)

            if decision["decision"] == "archive":
                archived += 1
                await _archive_entry(entry["id"], decision["reason"], context)
            elif decision["decision"] == "update":
                updated += 1
                await _flag_for_update(entry["id"], decision, context)
            else:
                kept += 1

        except Exception as e:
            logger.error(f"Failed to evaluate entry {entry.get('id')}: {e}")
            details.append(
                {
                    "entry_id": entry.get("id"),
                    "decision": "keep",
                    "error": str(e),
                }
            )
            kept += 1

    logger.info(
        f"Obsolescence detection complete: "
        f"{archived} archived, {updated} flagged for update, {kept} kept"
    )

    return ObsolescenceResult(
        entries_reviewed=len(candidates),
        entries_archived=archived,
        entries_updated=updated,
        entries_kept=kept,
        details=details,
    )


async def _find_unused_entries(
    days_threshold: int,
    context: ObsolescenceContext,
) -> list[dict[str, Any]]:
    """Find entries not applied in threshold days.

    Args:
        days_threshold: Number of days without application.
        context: Execution context.

    Returns:
        List of unused entry dicts.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days_threshold)

    try:
        entries = await context.knowledge_store_client.list_entries(
            filters={
                "status": "active",
                "last_applied_before": cutoff.isoformat(),
            },
            limit=100,
        )
        return [e.model_dump() for e in entries]
    except Exception as e:
        logger.warning(f"Error fetching unused entries: {e}")
        return []


async def _find_failing_entries(
    context: ObsolescenceContext,
    min_applications: int = 3,
) -> list[dict[str, Any]]:
    """Find entries with low success rate.

    Args:
        context: Execution context.
        min_applications: Minimum applications before considering.

    Returns:
        List of failing entry dicts.
    """
    threshold = context.settings.thresholds.success_rate_archive

    try:
        entries = await context.knowledge_store_client.list_entries(
            filters={"status": "active"},
            limit=200,
        )

        # Filter to those with low success rate and enough applications
        failing = []
        for entry in entries:
            if entry.times_applied >= min_applications:
                success_rate = entry.success_count / entry.times_applied
                if success_rate <= threshold:
                    failing.append(entry.model_dump())

        return failing

    except Exception as e:
        logger.warning(f"Error fetching failing entries: {e}")
        return []


async def _evaluate_entry(
    entry: dict[str, Any],
    context: ObsolescenceContext,
) -> dict[str, Any]:
    """Evaluate single entry for obsolescence.

    Args:
        entry: Entry dict to evaluate.
        context: Execution context.

    Returns:
        Decision dict with decision, confidence, reason, etc.
    """
    # Build content for prompt
    content = {
        "pattern": entry.get("problem_pattern", ""),
        "solution": entry.get("solution", ""),
        "tags": entry.get("tags", []),
    }

    # Calculate success rate
    times_applied = entry.get("times_applied", 0)
    success_count = entry.get("success_count", 0)
    success_rate = success_count / times_applied if times_applied > 0 else 0

    prompt = format_obsolescence_prompt(
        entry_id=entry["id"],
        content=content,
        created_at=entry.get("created_at", "unknown"),
        last_applied=entry.get("last_applied"),
        success_rate=success_rate,
        recent_outcomes=[],  # Would need outcome history from knowledge-store
    )

    response_dict, usage = await context.llm_client.complete_json(
        prompt=prompt,
        system=OBSOLESCENCE_SYSTEM_PROMPT,
        task_type="obsolescence",
    )

    try:
        response = LLMObsolescenceResponse(**response_dict)
    except Exception as e:
        logger.error(f"Failed to parse LLM obsolescence response: {e}")
        # Conservative default: keep
        response = LLMObsolescenceResponse(
            decision="keep",
            confidence=0.5,
            reason=f"Parse error - defaulting to keep: {e}",
        )

    return {
        "entry_id": entry["id"],
        "decision": response.decision,
        "confidence": response.confidence,
        "reason": response.reason,
        "update_suggestion": response.update_suggestion,
        "indicators": response.obsolescence_indicators,
        "tokens_used": usage.total_tokens,
    }


async def _archive_entry(
    entry_id: str,
    reason: str,
    context: ObsolescenceContext,
) -> None:
    """Archive an obsolete entry.

    Args:
        entry_id: Entry to archive.
        reason: Reason for archival.
        context: Execution context.
    """
    try:
        success = await context.knowledge_store_client.update_entry(
            entry_id=entry_id,
            updates={
                "status": "archived",
                "archive_reason": reason,
                "archived_at": datetime.now(UTC).isoformat(),
            },
        )
        if success:
            logger.info(f"Archived entry {entry_id}")
        else:
            logger.warning(f"Failed to archive entry {entry_id}")
    except Exception as e:
        logger.error(f"Error archiving entry {entry_id}: {e}")


async def _flag_for_update(
    entry_id: str,
    decision: dict[str, Any],
    context: ObsolescenceContext,
) -> None:
    """Flag entry for manual update.

    Args:
        entry_id: Entry to flag.
        decision: Decision dict with update_suggestion.
        context: Execution context.
    """
    try:
        success = await context.knowledge_store_client.update_entry(
            entry_id=entry_id,
            updates={
                "needs_update": True,
                "update_suggestion": decision.get("update_suggestion"),
                "update_flagged_at": datetime.now(UTC).isoformat(),
            },
        )
        if success:
            logger.info(f"Flagged entry {entry_id} for update")
        else:
            logger.warning(f"Failed to flag entry {entry_id} for update")
    except Exception as e:
        logger.error(f"Error flagging entry {entry_id} for update: {e}")
