"""Structured logging configuration for Knowledge Curator.

Provides:
- JSON format logging for production
- Human-readable format for development
- Contextual logging with bind()
- Log correlation via task_id
"""

import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from loguru import logger

from knowledge_curator.config import Settings

if TYPE_CHECKING:
    pass

# Context variables for log correlation
_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)
_task_type: ContextVar[str | None] = ContextVar("task_type", default=None)


def get_task_context() -> dict[str, Any]:
    """Get current task context for logging."""
    return {
        "task_id": _task_id.get(),
        "task_type": _task_type.get(),
    }


def set_task_context(task_id: str | None = None, task_type: str | None = None) -> None:
    """Set task context for logging correlation.

    Args:
        task_id: Current task ID.
        task_type: Current task type.
    """
    _task_id.set(task_id)
    _task_type.set(task_type)


def clear_task_context() -> None:
    """Clear task context after task completion."""
    _task_id.set(None)
    _task_type.set(None)


def _json_sink(message: Any) -> None:
    """Write log record as JSON to stderr.

    This is a sink function (not a format function) to avoid loguru's
    format string processing which interprets JSON braces as placeholders.

    Args:
        message: Loguru message object with .record attribute.
    """
    import json
    from datetime import UTC, datetime

    record = message.record

    # Build structured log entry
    log_entry: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "file": record["file"].name,
    }

    # Add task context if available
    task_context = get_task_context()
    if task_context["task_id"]:
        log_entry["task_id"] = task_context["task_id"]
    if task_context["task_type"]:
        log_entry["task_type"] = task_context["task_type"]

    # Add exception info if present
    exception = record["exception"]
    if exception:
        log_entry["exception"] = {
            "type": exception.type.__name__ if exception.type else "Unknown",
            "value": str(exception.value) if exception.value else "",
            "traceback": bool(exception.traceback),
        }

    # Add any extra data bound to the logger
    extra = record["extra"]
    if extra:
        log_entry["extra"] = {k: v for k, v in extra.items() if not k.startswith("_")}

    sys.stderr.write(json.dumps(log_entry) + "\n")
    sys.stderr.flush()


# Format string for human-readable output
_HUMAN_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "{message}"
    "\n{exception}"
)


def configure_logging(settings: Settings) -> None:
    """Configure logging based on settings.

    Args:
        settings: Application settings.
    """
    # Remove default handler
    logger.remove()

    # Determine if we should use JSON format
    # Use JSON in production (when not DEBUG level and not a TTY)
    use_json = settings.log_level.upper() != "DEBUG" and not sys.stderr.isatty()

    # Console handler
    if use_json:
        # Use sink function instead of format to avoid loguru interpreting
        # JSON braces as format placeholders (KeyError: '"timestamp"')
        logger.add(
            _json_sink,
            level=settings.log_level,
            colorize=False,
        )
    else:
        logger.add(
            sys.stderr,
            format=_HUMAN_FORMAT,
            level=settings.log_level,
            colorize=True,
        )

    # File handler (always JSON for parsing)
    # Use loguru's built-in serialize=True for file handlers
    logger.add(
        "logs/curator.log",
        serialize=True,
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        level=settings.log_level,
    )

    # Error file (for alerting)
    logger.add(
        "logs/curator.error.log",
        serialize=True,
        rotation="10 MB",
        retention="30 days",
        compression="gz",
        level="ERROR",
        filter=lambda record: record["level"].name in ("ERROR", "CRITICAL"),
    )

    logger.info(f"Logging configured: level={settings.log_level}, json={use_json}")


class TaskLogger:
    """Context manager for task-scoped logging.

    Usage:
        async with TaskLogger(task_id, task_type) as log:
            log.info("Processing task")
            # ... do work ...
    """

    def __init__(self, task_id: str, task_type: str) -> None:
        """Initialize task logger.

        Args:
            task_id: Task ID for correlation.
            task_type: Task type for categorization.
        """
        self.task_id = task_id
        self.task_type = task_type
        self._logger = logger.bind(task_id=task_id, task_type=task_type)

    async def __aenter__(self) -> "TaskLogger":
        """Enter context and set task context."""
        set_task_context(self.task_id, self.task_type)
        self._logger.debug(f"Starting task: {self.task_type}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit context and clear task context."""
        if exc_val:
            self._logger.error(f"Task failed: {exc_val}")
        else:
            self._logger.debug(f"Completed task: {self.task_type}")
        clear_task_context()

    def debug(self, message: str, **kwargs: Any) -> None:
        """Log debug message."""
        self._logger.debug(message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """Log info message."""
        self._logger.info(message, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        """Log warning message."""
        self._logger.warning(message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Log error message."""
        self._logger.error(message, **kwargs)

    def exception(self, message: str, **kwargs: Any) -> None:
        """Log exception with traceback."""
        self._logger.exception(message, **kwargs)
