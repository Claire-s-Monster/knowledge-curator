"""Gap analysis task for Knowledge Curator.

Implements FR-2.5: Weekly gap analysis on Sunday 5 AM.
Identifies knowledge gaps from failed search queries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, Field

from knowledge_curator.clients import KnowledgeBridgeClient, KnowledgeStoreClient
from knowledge_curator.config import Settings
from knowledge_curator.core.models import GapAnalysisResult, GapCluster
from knowledge_curator.llm.client import CuratorLLMClient
from knowledge_curator.llm.prompts import GAP_SYSTEM_PROMPT, format_gap_analysis_prompt

logger = logging.getLogger(__name__)


class GapPayload(BaseModel):
    """Payload for identify_gaps task."""

    triggered_by: str = Field(default="scheduler", description="Task trigger source")
    hours_lookback: int = Field(
        default=168, description="Hours to analyze (default: 1 week = 168)"
    )


@dataclass
class GapContext:
    """Dependencies for gap analysis task."""

    settings: Settings
    llm_client: CuratorLLMClient
    knowledge_store_client: KnowledgeStoreClient
    bridge_client: KnowledgeBridgeClient


class LLMGapResponse(BaseModel):
    """Parsed response from LLM gap analysis."""

    gap_clusters: list[dict[str, Any]] = Field(
        default_factory=list, description="Identified gap clusters"
    )
    summary: str = Field(description="Overall summary of knowledge gaps")
    recommendations: list[str] = Field(
        default_factory=list, description="Actionable recommendations"
    )


async def identify_gaps(
    payload: GapPayload,
    context: GapContext,
) -> GapAnalysisResult:
    """Run gap analysis batch.

    Workflow:
    1. Fetch search logs from knowledge-bridge (failed queries)
    2. Extract and aggregate failed queries
    3. Get existing categories for context
    4. LLM generates gap report with recommendations
    5. Return structured analysis result

    Args:
        payload: Task configuration.
        context: Execution context with clients.

    Returns:
        GapAnalysisResult with identified gaps and recommendations.
    """
    logger.info(f"Starting gap analysis (lookback: {payload.hours_lookback}h)")

    # Step 1: Fetch search logs from knowledge-bridge
    try:
        search_logs = await context.bridge_client.get_search_logs(
            hours=payload.hours_lookback,
            limit=1000,
        )
    except Exception as e:
        logger.error(f"Failed to fetch search logs: {e}")
        return GapAnalysisResult(
            period=_get_period_string(),
            total_searches=0,
            failed_searches=0,
            gap_clusters=[],
            recommendations=[f"Could not fetch search logs: {e}"],
        )

    if not search_logs:
        logger.info("No search logs to analyze")
        return GapAnalysisResult(
            period=_get_period_string(),
            total_searches=0,
            failed_searches=0,
            gap_clusters=[],
            recommendations=["No search data available for analysis"],
        )

    total_searches = len(search_logs)
    logger.debug(f"Fetched {total_searches} search logs")

    # Step 2: Filter to failed/low-quality searches
    failed_queries = _extract_failed_queries(search_logs)

    if not failed_queries:
        logger.info("No failed queries found")
        return GapAnalysisResult(
            period=_get_period_string(),
            total_searches=total_searches,
            failed_searches=0,
            gap_clusters=[],
            recommendations=["Knowledge coverage appears comprehensive"],
        )

    logger.info(
        f"Found {len(failed_queries)} failed queries from {total_searches} total"
    )

    # Step 3: Get existing categories for context
    existing_categories = await _get_existing_categories(context)

    # Step 4: LLM analysis
    prompt = format_gap_analysis_prompt(
        failed_queries=failed_queries,
        existing_categories=existing_categories,
    )

    try:
        response_dict, usage = await context.llm_client.complete_json(
            prompt=prompt,
            system=GAP_SYSTEM_PROMPT,
            task_type="gaps",
        )
    except Exception as e:
        logger.error(f"LLM analysis failed: {e}")
        return GapAnalysisResult(
            period=_get_period_string(),
            total_searches=total_searches,
            failed_searches=len(failed_queries),
            gap_clusters=[],
            recommendations=[f"LLM analysis failed: {e}"],
        )

    try:
        response = LLMGapResponse(**response_dict)
    except Exception as e:
        logger.error(f"Failed to parse LLM gap response: {e}")
        response = LLMGapResponse(
            gap_clusters=[],
            summary=f"Analysis failed: {e}",
            recommendations=["Manual review required due to analysis error"],
        )

    # Step 5: Build result with proper models
    gap_clusters = _build_gap_clusters(response.gap_clusters)

    logger.info(
        f"Gap analysis complete: {len(gap_clusters)} clusters, "
        f"{len(response.recommendations)} recommendations"
    )

    return GapAnalysisResult(
        period=_get_period_string(),
        total_searches=total_searches,
        failed_searches=len(failed_queries),
        gap_clusters=gap_clusters,
        recommendations=response.recommendations,
    )


def _get_period_string() -> str:
    """Get ISO week string for current period.

    Returns:
        Period string like "2026-W01".
    """
    now = datetime.now(UTC)
    return f"{now.year}-W{now.isocalendar()[1]:02d}"


def _extract_failed_queries(
    search_logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract and aggregate failed queries from search logs.

    Args:
        search_logs: Raw search log entries from bridge.

    Returns:
        List of aggregated failed query dicts with counts.
    """
    # Group queries by normalized text and count occurrences
    query_counts: dict[str, dict[str, Any]] = {}

    for log in search_logs:
        query = log.get("query", "").strip()
        if not query:
            continue

        query_lower = query.lower()
        results_count = log.get("results_count", 0)
        avg_similarity = log.get("avg_similarity", 1.0)

        # Consider "failed" if 0 results or very low similarity
        is_failed = results_count == 0 or avg_similarity < 0.5

        if is_failed:
            if query_lower not in query_counts:
                query_counts[query_lower] = {
                    "query": query,  # Keep original case
                    "count": 0,
                    "avg_results": 0.0,
                }
            query_counts[query_lower]["count"] += 1
            # Running average
            n = query_counts[query_lower]["count"]
            prev_avg = query_counts[query_lower]["avg_results"]
            query_counts[query_lower]["avg_results"] = (
                prev_avg * (n - 1) + results_count
            ) / n

    # Sort by count descending
    return sorted(
        query_counts.values(),
        key=lambda x: x["count"],
        reverse=True,
    )


async def _get_existing_categories(
    context: GapContext,
) -> list[str]:
    """Get list of existing pattern types/categories.

    Args:
        context: Execution context.

    Returns:
        List of category names.
    """
    try:
        stats = await context.knowledge_store_client.get_stats()
        return cast(list[str], stats.get("pattern_types", []))
    except Exception as e:
        logger.warning(f"Failed to get existing categories: {e}")
        return []


def _build_gap_clusters(
    raw_clusters: list[dict[str, Any]],
) -> list[GapCluster]:
    """Build GapCluster models from raw LLM response.

    Args:
        raw_clusters: Raw cluster dicts from LLM response.

    Returns:
        List of validated GapCluster models.
    """
    clusters = []
    for c in raw_clusters:
        try:
            # Handle different response formats
            theme = c.get("theme", "")
            queries = c.get("queries", [])
            if not theme and queries:
                theme = queries[0]

            suggested_entry = c.get("suggested_entry", {})
            pattern_type = (
                suggested_entry.get("pattern_type", "unknown")
                if isinstance(suggested_entry, dict)
                else "unknown"
            )

            clusters.append(
                GapCluster(
                    representative_query=theme,
                    similar_queries=queries,
                    frequency=c.get("frequency", len(queries)),
                    suggested_pattern_type=pattern_type,
                )
            )
        except Exception as e:
            logger.warning(f"Failed to build GapCluster: {e}")
            continue

    return clusters
