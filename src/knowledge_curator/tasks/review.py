"""Review task for evaluating staged knowledge entries.

Implements FR-2.1 from PRD: review_staged_entry task that:
1. Fetches entry details from knowledge-bridge
2. Searches UCKN for similar entries
3. Uses LLM to evaluate quality and novelty
4. Makes promote/reject/merge/flag decision
5. Notifies knowledge-bridge of the decision
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from knowledge_curator.clients import (
    CurationDecision,
    KnowledgeBridgeClient,
    StagedEntry,
    UCKNClient,
)
from knowledge_curator.config import Settings
from knowledge_curator.core.models import DecisionType, ReviewDecision
from knowledge_curator.llm.client import CuratorLLMClient
from knowledge_curator.llm.prompts import REVIEW_SYSTEM_PROMPT, format_review_prompt

logger = logging.getLogger(__name__)


class ReviewPayload(BaseModel):
    """Payload for review_staged_entry task."""

    entry_id: str = Field(description="Staged entry ID to review")
    priority: int = Field(default=1, description="Task priority")


class LLMReviewResponse(BaseModel):
    """Parsed response from LLM review."""

    decision: Literal["promote", "reject", "merge", "flag_human"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    merge_with: str | None = None
    merged_content: dict[str, Any] | None = None
    quality_score: float = Field(default=0.5, ge=0.0, le=1.0)
    novelty_score: float = Field(default=0.5, ge=0.0, le=1.0)
    generalizability_score: float = Field(default=0.5, ge=0.0, le=1.0)


@dataclass
class ReviewContext:
    """Dependencies for review task execution."""

    settings: Settings
    llm_client: CuratorLLMClient
    uckn_client: UCKNClient
    bridge_client: KnowledgeBridgeClient


async def review_staged_entry(
    payload: ReviewPayload,
    context: ReviewContext,
) -> ReviewDecision:
    """Review a staged entry and make a curation decision.

    This is the main entry point for the review task, called by the
    task queue processor.

    Args:
        payload: Task payload with entry_id.
        context: Execution context with clients.

    Returns:
        ReviewDecision with the decision and metadata.

    Raises:
        ValueError: If entry not found or LLM response invalid.
    """
    entry_id = payload.entry_id
    logger.info(f"Starting review for entry {entry_id}")

    # Step 1: Fetch entry from knowledge-bridge
    entry = await context.bridge_client.get_staged_entry(entry_id)
    if entry is None:
        raise ValueError(f"Entry not found: {entry_id}")

    # Step 2: Search for similar entries in UCKN
    similar_entries = await _search_similar(entry, context)

    # Step 3: Apply similarity-based pre-filtering
    prefilter_result = _prefilter_by_similarity(similar_entries, context.settings)
    if prefilter_result is not None:
        logger.info(f"Entry {entry_id} pre-filtered: {prefilter_result.decision}")
        # Notify bridge and return early
        await _notify_decision(entry_id, prefilter_result, context)
        return prefilter_result

    # Step 4: LLM evaluation for novel/partial overlap cases
    llm_response = await _llm_evaluate(entry, similar_entries, context)

    # Step 5: Build final decision
    decision = _build_decision(entry, llm_response, similar_entries, context)

    # Step 6: Notify knowledge-bridge
    await _notify_decision(entry_id, decision, context)

    # Step 7: Execute decision (promote to UCKN if needed)
    if decision.decision == DecisionType.PROMOTE:
        await _promote_to_uckn(entry, context)
    elif decision.decision == DecisionType.MERGE and decision.merged_content:
        await _merge_with_existing(
            entry,
            llm_response.merge_with,  # type: ignore
            decision.merged_content,
            context,
        )

    logger.info(
        f"Completed review for {entry_id}: {decision.decision} "
        f"(confidence: {decision.confidence:.2f})"
    )

    return decision


async def _search_similar(
    entry: StagedEntry,
    context: ReviewContext,
) -> list[dict[str, Any]]:
    """Search UCKN for similar entries.

    Args:
        entry: The staged entry to find similar entries for.
        context: Execution context.

    Returns:
        List of similar entries with similarity scores.
    """
    # Build search query from entry content
    content = entry.content
    query_parts = []

    if "pattern" in content:
        query_parts.append(content["pattern"])
    if "problem" in content:
        query_parts.append(content["problem"])
    if "solution" in content:
        query_parts.append(content["solution"][:500])  # Limit solution length

    query = " ".join(query_parts) or str(content)[:1000]

    try:
        results = await context.uckn_client.search_similar(
            query=query,
            limit=10,
            min_similarity=context.settings.thresholds.similarity_related,
        )

        return [
            {
                "id": r.entry.id,
                "content": {
                    "problem_pattern": r.entry.problem_pattern,
                    "solution": r.entry.solution,
                },
                "similarity": r.similarity_score,
                "metadata": {
                    "tags": r.entry.tags,
                    "pattern_type": r.entry.pattern_type,
                    "quality_score": r.entry.quality_score,
                },
            }
            for r in results
        ]

    except Exception as e:
        logger.warning(f"UCKN search failed: {e}")
        return []


def _prefilter_by_similarity(
    similar_entries: list[dict[str, Any]],
    settings: Settings,
) -> ReviewDecision | None:
    """Pre-filter obvious duplicates without LLM.

    According to PRD decision tree:
    - similarity > 0.95 → DUPLICATE (reject)

    Args:
        similar_entries: Similar entries from UCKN.
        settings: Application settings.

    Returns:
        ReviewDecision if pre-filtered, None if LLM evaluation needed.
    """
    if not similar_entries:
        return None

    # Check for near-duplicates
    threshold = settings.thresholds.similarity_duplicate

    for entry in similar_entries:
        similarity = entry.get("similarity", 0)
        if similarity >= threshold:
            return ReviewDecision(
                entry_id="",  # Will be filled by caller
                decision=DecisionType.REJECT,
                confidence=similarity,
                reason=(
                    f"Near-duplicate of existing entry {entry['id']} "
                    f"(similarity: {similarity:.2f})"
                ),
                similar_entries=[entry["id"]],
                model_used="prefilter",
                tokens_input=0,
                tokens_output=0,
            )

    return None


async def _llm_evaluate(
    entry: StagedEntry,
    similar_entries: list[dict[str, Any]],
    context: ReviewContext,
) -> LLMReviewResponse:
    """Use LLM to evaluate entry quality and make decision.

    Args:
        entry: Staged entry to evaluate.
        similar_entries: Similar existing entries.
        context: Execution context.

    Returns:
        Parsed LLM response.
    """
    # Format prompt
    prompt = format_review_prompt(
        entry_id=entry.id,
        content=entry.content,
        source=entry.source,
        session_id=entry.source_id,
        similar_entries=similar_entries,
    )

    # Call LLM
    response_dict, usage = await context.llm_client.complete_json(
        prompt=prompt,
        system=REVIEW_SYSTEM_PROMPT,
        task_type="review",
    )

    # Parse response
    try:
        response = LLMReviewResponse(**response_dict)
    except Exception as e:
        logger.error(f"Failed to parse LLM response: {e}")
        # Default to flag for human review
        response = LLMReviewResponse(
            decision="flag_human",
            confidence=0.5,
            reason=f"LLM response parsing failed: {e}",
        )

    logger.debug(
        f"LLM evaluation: {response.decision} "
        f"(tokens: {usage.input_tokens}+{usage.output_tokens})"
    )

    return response


def _build_decision(
    entry: StagedEntry,
    llm_response: LLMReviewResponse,
    similar_entries: list[dict[str, Any]],
    context: ReviewContext,
) -> ReviewDecision:
    """Build final ReviewDecision from LLM response.

    Args:
        entry: Staged entry.
        llm_response: Parsed LLM response.
        similar_entries: Similar entries found.
        context: Execution context.

    Returns:
        ReviewDecision with all metadata.
    """
    # Map LLM decision string to DecisionType enum
    decision_map = {
        "promote": DecisionType.PROMOTE,
        "reject": DecisionType.REJECT,
        "merge": DecisionType.MERGE,
        "flag_human": DecisionType.FLAG_HUMAN,
    }

    decision_type = decision_map.get(llm_response.decision, DecisionType.FLAG_HUMAN)

    return ReviewDecision(
        entry_id=entry.id,
        decision=decision_type,
        confidence=llm_response.confidence,
        reason=llm_response.reason,
        similar_entries=[e["id"] for e in similar_entries],
        merged_content=llm_response.merged_content,
        model_used=context.settings.models.default,
        tokens_input=0,  # Would need to track from LLM call
        tokens_output=0,
    )


async def _notify_decision(
    entry_id: str,
    decision: ReviewDecision,
    context: ReviewContext,
) -> None:
    """Notify knowledge-bridge of the curation decision.

    Args:
        entry_id: Entry ID.
        decision: The decision made.
        context: Execution context.
    """
    # Map DecisionType to API decision string
    decision_str: Literal["promote", "reject", "merge", "flag_human"]
    if decision.decision == DecisionType.PROMOTE:
        decision_str = "promote"
    elif decision.decision == DecisionType.REJECT:
        decision_str = "reject"
    elif decision.decision == DecisionType.MERGE:
        decision_str = "merge"
    else:
        decision_str = "flag_human"

    curation_decision = CurationDecision(
        entry_id=entry_id,
        decision=decision_str,
        reason=decision.reason,
        confidence=decision.confidence,
        merged_content=decision.merged_content,
        similar_entries=decision.similar_entries,
    )

    success = await context.bridge_client.notify_decision(curation_decision)
    if not success:
        logger.warning(f"Failed to notify bridge of decision for {entry_id}")


async def _promote_to_uckn(
    entry: StagedEntry,
    context: ReviewContext,
) -> str | None:
    """Promote entry to UCKN knowledge base.

    Args:
        entry: Entry to promote.
        context: Execution context.

    Returns:
        UCKN pattern ID if successful, None otherwise.
    """
    content = entry.content

    # Build metadata for UCKN
    metadata = {
        "title": content.get("pattern", content.get("problem", "Unknown pattern")),
        "description": content.get("solution", "")[:500],
        "pattern_type": content.get("type", "debugging"),
        "tags": content.get("tags", []),
        "source": entry.source,
        "source_id": entry.source_id,
        "technology_stack": content.get("context", {}),
    }

    # Document is the full content
    document = _build_document(content)

    pattern_id = await context.uckn_client.contribute_pattern(
        document=document,
        metadata=metadata,
        project_id=content.get("project_id"),
    )

    if pattern_id:
        logger.info(f"Promoted entry {entry.id} to UCKN as {pattern_id}")
    else:
        logger.warning(f"Failed to promote entry {entry.id} to UCKN")

    return pattern_id


async def _merge_with_existing(
    entry: StagedEntry,
    merge_with_id: str,
    merged_content: dict[str, Any],
    context: ReviewContext,
) -> bool:
    """Merge entry content with existing UCKN entry.

    Args:
        entry: New entry with additional content.
        merge_with_id: ID of existing entry to merge with.
        merged_content: LLM-generated merged content.
        context: Execution context.

    Returns:
        True if merge succeeded.
    """
    # Get existing entry
    existing = await context.uckn_client.get_pattern(merge_with_id)
    if existing is None:
        logger.warning(f"Merge target not found: {merge_with_id}")
        return False

    # Update with merged content
    updates = {
        "document": _build_document(merged_content),
        "metadata": {
            **existing.get("metadata", {}),
            "merged_from": entry.id,
        },
    }

    success = await context.uckn_client.update_pattern(merge_with_id, updates)
    if success:
        logger.info(f"Merged entry {entry.id} into {merge_with_id}")
    else:
        logger.warning(f"Failed to merge entry {entry.id} into {merge_with_id}")

    return success


def _build_document(content: dict[str, Any]) -> str:
    """Build a document string from content dict.

    Args:
        content: Content dictionary.

    Returns:
        Formatted document string.
    """
    parts = []

    if "pattern" in content:
        parts.append(f"# Pattern\n{content['pattern']}")
    if "problem" in content:
        parts.append(f"# Problem\n{content['problem']}")
    if "solution" in content:
        parts.append(f"# Solution\n{content['solution']}")
    if "context" in content:
        ctx = content["context"]
        if isinstance(ctx, dict):
            ctx_str = "\n".join(f"- {k}: {v}" for k, v in ctx.items())
            parts.append(f"# Context\n{ctx_str}")
        else:
            parts.append(f"# Context\n{ctx}")
    if "tags" in content and content["tags"]:
        parts.append(f"# Tags\n{', '.join(content['tags'])}")

    return "\n\n".join(parts) if parts else str(content)
