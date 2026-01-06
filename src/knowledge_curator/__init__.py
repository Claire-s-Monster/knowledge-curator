"""Knowledge Curator - LLM-powered knowledge curation daemon for UCKN."""

__version__ = "0.1.0"

from knowledge_curator.logging import (
    TaskLogger,
    clear_task_context,
    configure_logging,
    set_task_context,
)

__all__ = [
    "__version__",
    "TaskLogger",
    "configure_logging",
    "set_task_context",
    "clear_task_context",
]
