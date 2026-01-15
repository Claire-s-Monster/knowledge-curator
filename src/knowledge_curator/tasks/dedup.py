"""Deduplication task for Knowledge Curator.

Implements FR-2.3: Hourly deduplication batch.
Finds and resolves duplicate entries in knowledge-store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from knowledge_curator.clients import KnowledgeStoreClient
from knowledge_curator.config import Settings
from knowledge_curator.core.models import DeduplicationResult
from knowledge_curator.llm.client import CuratorLLMClient
from knowledge_curator.llm.prompts import DEDUP_SYSTEM_PROMPT, format_dedup_prompt

logger = logging.getLogger(__name__)


class DedupPayload(BaseModel):
    """Payload for deduplicate task."""

    triggered_by: str = Field(default="scheduler", description="Task trigger source")
    hours_lookback: int = Field(
        default=24, description="Hours to look back for modified entries"
    )


@dataclass
class DedupContext:
    """Dependencies for deduplication task."""

    settings: Settings
    llm_client: CuratorLLMClient
    knowledge_store_client: KnowledgeStoreClient


class LLMDedupResponse(BaseModel):
    """Parsed response from LLM deduplication."""

    canonical_id: str = Field(description="ID of the best entry")
    merge_from: list[str] = Field(
        default_factory=list, description="IDs to merge content from"
    )
    superseded_ids: list[str] = Field(
        default_factory=list, description="IDs to mark as superseded"
    )
    merged_content: dict[str, Any] | None = Field(
        default=None, description="Merged content if applicable"
    )
    reason: str = Field(description="Explanation of the decision")


async def deduplicate(
    payload: DedupPayload,
    context: DedupContext,
) -> DeduplicationResult:
    """Run deduplication batch.

    Workflow:
    1. Fetch entries modified in last N hours
    2. Find clusters with similarity > threshold
    3. LLM selects canonical entry per cluster
    4. Mark inferior entries as superseded

    Args:
        payload: Task configuration.
        context: Execution context with clients.

    Returns:
        DeduplicationResult with cluster resolutions.
    """
    logger.info(f"Starting deduplication (lookback: {payload.hours_lookback}h)")

    # Step 1: Fetch recent entries
    entries = await context.knowledge_store_client.get_recent_entries(
        hours=payload.hours_lookback,
        limit=500,
    )

    if not entries:
        logger.info("No recent entries to deduplicate")
        return DeduplicationResult(
            clusters_found=0,
            entries_superseded=0,
            entries_kept=0,
            details=[],
        )

    logger.debug(f"Found {len(entries)} recent entries")

    # Step 2: Find duplicate clusters using embedding similarity
    clusters = await _find_duplicate_clusters(entries, context)

    if not clusters:
        logger.info("No duplicate clusters found")
        return DeduplicationResult(
            clusters_found=0,
            entries_superseded=0,
            entries_kept=len(entries),
            details=[],
        )

    logger.info(f"Found {len(clusters)} duplicate clusters")

    # Step 3: Resolve each cluster with LLM
    details: list[dict[str, Any]] = []
    entries_superseded = 0
    entries_kept = 0

    for cluster in clusters:
        try:
            resolution = await _resolve_cluster(cluster, context)
            details.append(resolution)
            entries_superseded += len(resolution.get("superseded_ids", []))
            entries_kept += 1  # One kept per cluster
        except Exception as e:
            logger.error(f"Failed to resolve cluster: {e}")
            # Keep all entries in failed clusters
            entries_kept += len(cluster)
            details.append(
                {
                    "cluster_size": len(cluster),
                    "error": str(e),
                    "superseded_ids": [],
                }
            )

    # Step 4: Apply supersession updates
    for detail in details:
        if "error" not in detail:
            await _apply_supersession(detail, context)

    logger.info(
        f"Deduplication complete: {len(clusters)} clusters, "
        f"{entries_superseded} superseded, {entries_kept} kept"
    )

    return DeduplicationResult(
        clusters_found=len(clusters),
        entries_superseded=entries_superseded,
        entries_kept=entries_kept,
        details=details,
    )


async def _find_duplicate_clusters(
    entries: list[dict[str, Any]],
    context: DedupContext,
) -> list[list[dict[str, Any]]]:
    """Find clusters of similar entries.

    Uses knowledge-store's find_similar for each entry,
    then groups entries that are similar to each other.

    Args:
        entries: List of entry dicts to check.
        context: Execution context.

    Returns:
        List of clusters, each cluster is a list of similar entries.
    """
    threshold = context.settings.thresholds.similarity_duplicate
    seen_ids: set[str] = set()
    clusters: list[list[dict[str, Any]]] = []

    for entry in entries:
        entry_id = entry.get("id")
        if not entry_id or entry_id in seen_ids:
            continue

        try:
            # Find similar entries
            similar = await context.knowledge_store_client.find_similar(
                entry_id=entry_id,
                threshold=threshold,
                limit=10,
            )

            if len(similar) > 0:  # Found similar entries
                cluster = [entry]
                for result in similar:
                    if result.entry.id != entry_id and result.entry.id not in seen_ids:
                        cluster.append(result.entry.model_dump())
                        seen_ids.add(result.entry.id)

                if len(cluster) > 1:
                    clusters.append(cluster)

            seen_ids.add(entry_id)

        except Exception as e:
            logger.warning(f"Error finding similar entries for {entry_id}: {e}")
            seen_ids.add(entry_id)

    return clusters


async def _resolve_cluster(
    cluster: list[dict[str, Any]],
    context: DedupContext,
) -> dict[str, Any]:
    """Use LLM to select canonical entry from cluster.

    Args:
        cluster: List of similar entries.
        context: Execution context.

    Returns:
        Resolution dict with canonical_id, superseded_ids, etc.
    """
    # Enrich entries with success rate for LLM context
    enriched_entries = []
    for entry in cluster:
        times_applied = entry.get("times_applied", 0)
        success_count = entry.get("success_count", 0)
        success_rate = success_count / times_applied if times_applied > 0 else 0

        enriched_entries.append(
            {
                **entry,
                "success_rate": success_rate,
                "content": {
                    "pattern": entry.get("problem_pattern", ""),
                    "solution": entry.get("solution", ""),
                    "tags": entry.get("tags", []),
                },
            }
        )

    prompt = format_dedup_prompt(enriched_entries)

    # Log prompt details before LLM call
    logger.debug(
        f"DEDUP_DEBUG: About to call LLM - "
        f"prompt_length={len(prompt)}, "
        f"system_length={len(DEDUP_SYSTEM_PROMPT)}, "
        f"cluster_size={len(cluster)}"
    )

    response_dict, usage = await context.llm_client.complete_json(
        prompt=prompt,
        system=DEDUP_SYSTEM_PROMPT,
        task_type="dedup",
    )

    try:
        response = LLMDedupResponse(**response_dict)
    except Exception as e:
        logger.error(f"Failed to parse LLM dedup response: {e}")
        # Default: keep first entry (often oldest), supersede rest
        response = LLMDedupResponse(
            canonical_id=cluster[0]["id"],
            superseded_ids=[e["id"] for e in cluster[1:]],
            reason=f"Parse error fallback: {e}",
        )

    return {
        "cluster_size": len(cluster),
        "canonical_id": response.canonical_id,
        "superseded_ids": response.superseded_ids,
        "merged_content": response.merged_content,
        "reason": response.reason,
        "tokens_used": usage.total_tokens,
    }


async def _apply_supersession(
    resolution: dict[str, Any],
    context: DedupContext,
) -> None:
    """Apply supersession updates to knowledge-store.

    Args:
        resolution: Cluster resolution with canonical_id and superseded_ids.
        context: Execution context.
    """
    canonical_id = resolution.get("canonical_id")
    superseded_ids = resolution.get("superseded_ids", [])

    for entry_id in superseded_ids:
        try:
            success = await context.knowledge_store_client.update_entry(
                entry_id=entry_id,
                updates={
                    "status": "superseded",
                    "superseded_by": canonical_id,
                },
            )
            if success:
                logger.debug(f"Marked {entry_id} as superseded by {canonical_id}")
            else:
                logger.warning(f"Failed to mark {entry_id} as superseded")
        except Exception as e:
            logger.error(f"Error marking {entry_id} as superseded: {e}")

    # If we have merged content, update the canonical entry
    merged_content = resolution.get("merged_content")
    if merged_content and canonical_id:
        try:
            await context.knowledge_store_client.update_entry(
                entry_id=canonical_id,
                updates={"merged_content": merged_content},
            )
            logger.debug(f"Updated canonical entry {canonical_id} with merged content")
        except Exception as e:
            logger.warning(f"Failed to update canonical entry with merged content: {e}")
