"""Prompt templates for Knowledge Curator tasks.

Contains structured prompts for:
- Entry review and evaluation
- Duplicate detection and merging
- Obsolescence assessment
- Gap analysis
"""

from typing import Any

# =============================================================================
# REVIEW PROMPTS
# =============================================================================

REVIEW_SYSTEM_PROMPT = """You are a knowledge curator evaluating learning entries for a global knowledge base.

Your role is to ensure high-quality, generalizable knowledge is promoted while filtering out:
- Duplicates of existing entries
- Low-quality or incorrect solutions
- Overly specific entries that won't help future sessions

Be rigorous but fair. Good patterns deserve to be shared."""

REVIEW_ENTRY_PROMPT = """## Entry to Evaluate

**Entry ID**: {entry_id}
**Source**: {source}
**Session**: {session_id}

### Content
{content}

## Similar Existing Entries
{similar_entries}

## Evaluation Criteria

1. **Novelty**: Does this add new information not in existing entries?
2. **Quality**: Is the solution correct, complete, and well-explained?
3. **Generalizability**: Will this help future sessions with similar problems?
4. **Specificity**: Is it specific enough to be actionable?

## Decision Options

- **PROMOTE**: Novel, high-quality, generalizable - add to knowledge base
- **REJECT**: Duplicate, low quality, or too specific - do not add
- **MERGE**: Partial overlap - combine with existing entry
- **FLAG_HUMAN**: Unclear or requires domain expertise - needs human review

Respond with JSON:
```json
{{
    "decision": "promote|reject|merge|flag_human",
    "confidence": 0.0-1.0,
    "reason": "Brief explanation of your decision",
    "merge_with": "entry_id if merge, null otherwise",
    "merged_content": "Combined content if merge, null otherwise",
    "quality_score": 0.0-1.0,
    "novelty_score": 0.0-1.0,
    "generalizability_score": 0.0-1.0
}}
```"""


def format_review_prompt(
    entry_id: str,
    content: dict[str, Any],
    source: str,
    session_id: str | None,
    similar_entries: list[dict[str, Any]],
) -> str:
    """Format the review prompt with entry details.

    Args:
        entry_id: Entry identifier.
        content: Entry content dict.
        source: Source of the entry.
        session_id: Originating session.
        similar_entries: List of similar existing entries.

    Returns:
        Formatted prompt string.
    """
    # Format content as readable text
    content_text = _format_content(content)

    # Format similar entries
    if similar_entries:
        similar_text = "\n".join(
            f"### Entry {i + 1} (ID: {e.get('id', 'unknown')}, "
            f"Similarity: {e.get('similarity', 0):.2f})\n{_format_content(e.get('content', {}))}"
            for i, e in enumerate(similar_entries[:5])  # Limit to 5
        )
    else:
        similar_text = "_No similar entries found in the knowledge base._"

    return REVIEW_ENTRY_PROMPT.format(
        entry_id=entry_id,
        source=source,
        session_id=session_id or "unknown",
        content=content_text,
        similar_entries=similar_text,
    )


# =============================================================================
# DEDUPLICATION PROMPTS
# =============================================================================

DEDUP_SYSTEM_PROMPT = """You are a knowledge curator identifying and resolving duplicate entries.

When entries cover the same topic, you must:
1. Identify the highest quality version
2. Merge complementary information if beneficial
3. Mark inferior versions as superseded

Prefer entries that are:
- More complete and detailed
- More recently validated
- More generalizable"""

DEDUP_CLUSTER_PROMPT = """## Duplicate Cluster

The following entries appear to cover the same topic:

{entries}

## Task

Analyze these entries and determine:
1. Which entry is the best "canonical" version?
2. Should any content be merged from other entries?
3. Which entries should be marked as superseded?

Respond with JSON:
```json
{{
    "canonical_id": "ID of the best entry",
    "merge_from": ["IDs of entries to merge content from"],
    "superseded_ids": ["IDs of entries to mark as superseded"],
    "merged_content": "New content if merging, null otherwise",
    "reason": "Explanation of your decision"
}}
```"""


def format_dedup_prompt(entries: list[dict[str, Any]]) -> str:
    """Format the deduplication prompt with cluster entries.

    Args:
        entries: List of similar entries in the cluster.

    Returns:
        Formatted prompt string.
    """
    entries_text = "\n\n".join(
        f"### Entry {i + 1}\n**ID**: {e.get('id', 'unknown')}\n"
        f"**Created**: {e.get('created_at', 'unknown')}\n"
        f"**Success Rate**: {e.get('success_rate', 0):.0%}\n"
        f"**Times Applied**: {e.get('times_applied', 0)}\n\n"
        f"{_format_content(e.get('content', {}))}"
        for i, e in enumerate(entries)
    )

    return DEDUP_CLUSTER_PROMPT.format(entries=entries_text)


# =============================================================================
# OBSOLESCENCE PROMPTS
# =============================================================================

OBSOLESCENCE_SYSTEM_PROMPT = """You are a knowledge curator assessing whether solutions are still valid and useful.

Technology evolves rapidly. Your job is to identify entries that:
- Reference deprecated APIs or tools
- Describe workarounds for fixed bugs
- Use outdated patterns when better alternatives exist
- Have consistently failed in recent applications

Be conservative - only mark entries as obsolete when clearly outdated."""

OBSOLESCENCE_PROMPT = """## Entry to Assess

**Entry ID**: {entry_id}
**Created**: {created_at}
**Last Applied**: {last_applied}
**Success Rate**: {success_rate}
**Recent Outcomes**: {recent_outcomes}

### Content
{content}

## Assessment Criteria

1. **Technology Currency**: Are the tools/APIs/patterns still current?
2. **Effectiveness**: Has this solution been working recently?
3. **Better Alternatives**: Are there now superior approaches?
4. **Context Validity**: Is the original problem context still relevant?

Respond with JSON:
```json
{{
    "decision": "keep|update|archive",
    "confidence": 0.0-1.0,
    "reason": "Explanation of your assessment",
    "update_suggestion": "Suggested update if decision is 'update', null otherwise",
    "obsolescence_indicators": ["List of specific obsolescence indicators found"]
}}
```"""


def format_obsolescence_prompt(
    entry_id: str,
    content: dict[str, Any],
    created_at: str,
    last_applied: str | None,
    success_rate: float,
    recent_outcomes: list[str],
) -> str:
    """Format the obsolescence assessment prompt.

    Args:
        entry_id: Entry identifier.
        content: Entry content.
        created_at: Creation timestamp.
        last_applied: Last application timestamp.
        success_rate: Overall success rate.
        recent_outcomes: Recent outcome history.

    Returns:
        Formatted prompt string.
    """
    outcomes_text = ", ".join(recent_outcomes[-10:]) if recent_outcomes else "None"

    return OBSOLESCENCE_PROMPT.format(
        entry_id=entry_id,
        created_at=created_at,
        last_applied=last_applied or "Never",
        success_rate=f"{success_rate:.0%}",
        recent_outcomes=outcomes_text,
        content=_format_content(content),
    )


# =============================================================================
# GAP ANALYSIS PROMPTS
# =============================================================================

GAP_SYSTEM_PROMPT = """You are a knowledge curator analyzing search patterns to identify knowledge gaps.

Your role is to:
1. Identify common queries that found no good results
2. Cluster similar failed queries into themes
3. Suggest new knowledge entries that would fill these gaps

Focus on patterns that would benefit many future sessions."""

GAP_ANALYSIS_PROMPT = """## Failed Search Queries

The following queries returned no results or low-quality results:

{failed_queries}

## Existing Knowledge Categories

{existing_categories}

## Task

Analyze these failed queries and:
1. Cluster similar queries into themes
2. Identify the most impactful knowledge gaps
3. Suggest specific entries that would fill these gaps

Respond with JSON:
```json
{{
    "gap_clusters": [
        {{
            "theme": "Brief theme description",
            "queries": ["query1", "query2", ...],
            "frequency": 10,
            "suggested_entry": {{
                "pattern_type": "error|workflow|configuration|etc",
                "title": "Suggested entry title",
                "key_topics": ["topic1", "topic2"]
            }},
            "priority": "high|medium|low"
        }}
    ],
    "summary": "Overall summary of knowledge gaps",
    "recommendations": ["Actionable recommendations for improving coverage"]
}}
```"""


def format_gap_analysis_prompt(
    failed_queries: list[dict[str, Any]],
    existing_categories: list[str],
) -> str:
    """Format the gap analysis prompt.

    Args:
        failed_queries: List of failed search queries with metadata.
        existing_categories: List of existing knowledge categories.

    Returns:
        Formatted prompt string.
    """
    queries_text = "\n".join(
        f'- "{q.get("query", "")}" (count: {q.get("count", 1)}, '
        f"avg_results: {q.get('avg_results', 0):.1f})"
        for q in failed_queries[:50]  # Limit to 50
    )

    categories_text = (
        ", ".join(existing_categories) if existing_categories else "None defined"
    )

    return GAP_ANALYSIS_PROMPT.format(
        failed_queries=queries_text,
        existing_categories=categories_text,
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _format_content(content: dict[str, Any]) -> str:
    """Format entry content as readable text.

    Args:
        content: Content dictionary.

    Returns:
        Formatted text string.
    """
    if not content:
        return "_No content_"

    parts = []

    # Pattern/problem
    if "pattern" in content:
        parts.append(f"**Pattern**: {content['pattern']}")
    if "problem" in content:
        parts.append(f"**Problem**: {content['problem']}")

    # Solution
    if "solution" in content:
        parts.append(f"**Solution**: {content['solution']}")

    # Context
    if "context" in content:
        ctx = content["context"]
        if isinstance(ctx, dict):
            ctx_str = ", ".join(f"{k}: {v}" for k, v in ctx.items())
            parts.append(f"**Context**: {ctx_str}")
        else:
            parts.append(f"**Context**: {ctx}")

    # Tags
    if "tags" in content and content["tags"]:
        parts.append(f"**Tags**: {', '.join(content['tags'])}")

    # Fallback for other fields
    if not parts:
        for key, value in content.items():
            if isinstance(value, str) and len(value) < 500:
                parts.append(f"**{key.title()}**: {value}")

    return "\n".join(parts) if parts else str(content)
