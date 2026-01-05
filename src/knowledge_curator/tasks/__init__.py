"""Task implementations for curation operations.

Provides handlers for all curation task types:
- Phase 4: review_staged_entry
- Phase 5: process_feedback
- Phase 6: deduplicate, detect_obsolescence, identify_gaps
"""

from knowledge_curator.tasks.dedup import (
    DedupContext,
    DedupPayload,
    deduplicate,
)
from knowledge_curator.tasks.feedback import (
    FeedbackContext,
    FeedbackPayload,
    process_feedback,
)
from knowledge_curator.tasks.gaps import (
    GapContext,
    GapPayload,
    identify_gaps,
)
from knowledge_curator.tasks.obsolescence import (
    ObsolescenceContext,
    ObsolescencePayload,
    detect_obsolescence,
)
from knowledge_curator.tasks.review import (
    ReviewContext,
    ReviewPayload,
    review_staged_entry,
)

__all__ = [
    # Review task (Phase 4)
    "ReviewContext",
    "ReviewPayload",
    "review_staged_entry",
    # Feedback task (Phase 5)
    "FeedbackContext",
    "FeedbackPayload",
    "process_feedback",
    # Scheduled tasks (Phase 6)
    "DedupContext",
    "DedupPayload",
    "deduplicate",
    "ObsolescenceContext",
    "ObsolescencePayload",
    "detect_obsolescence",
    "GapContext",
    "GapPayload",
    "identify_gaps",
]
