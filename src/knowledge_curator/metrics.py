"""Prometheus metrics for Knowledge Curator.

Provides observability metrics as defined in the PRD:
- curator_tasks_processed (Counter)
- curator_task_duration_seconds (Histogram)
- curator_llm_tokens_used (Counter by model)
- curator_queue_depth (Gauge)
- curator_decisions (Counter by decision type)
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from knowledge_curator.core.queue import TaskQueue

# Prometheus client is optional - gracefully degrade if not available
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logger.warning("prometheus_client not installed - metrics disabled")


# Metric definitions (only created if prometheus_client is available)
if PROMETHEUS_AVAILABLE:
    # Task processing metrics
    TASKS_PROCESSED = Counter(
        "curator_tasks_processed_total",
        "Total number of tasks processed",
        ["task_type", "status"],
    )

    TASK_DURATION = Histogram(
        "curator_task_duration_seconds",
        "Task processing duration in seconds",
        ["task_type"],
        buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    )

    # LLM usage metrics
    LLM_TOKENS_USED = Counter(
        "curator_llm_tokens_total",
        "Total LLM tokens used",
        ["model", "direction"],  # direction: input or output
    )

    LLM_REQUESTS = Counter(
        "curator_llm_requests_total",
        "Total LLM API requests",
        ["model", "status"],  # status: success or error
    )

    LLM_COST_USD = Counter(
        "curator_llm_cost_usd_total",
        "Total estimated LLM cost in USD",
        ["model"],
    )

    # Queue metrics
    QUEUE_DEPTH = Gauge(
        "curator_queue_depth",
        "Current queue depth",
        ["status"],  # pending, processing, completed, failed
    )

    DLQ_DEPTH = Gauge(
        "curator_dlq_depth",
        "Current dead letter queue depth",
    )

    # Decision metrics
    DECISIONS = Counter(
        "curator_decisions_total",
        "Total curation decisions made",
        ["task_type", "decision"],
    )

    # Daemon status
    DAEMON_INFO = Gauge(
        "curator_daemon_info",
        "Daemon information",
        ["version"],
    )

    DAEMON_UP = Gauge(
        "curator_daemon_up",
        "Whether the daemon is up (1) or down (0)",
    )


class MetricsCollector:
    """Collects and exposes Prometheus metrics."""

    def __init__(self) -> None:
        """Initialize metrics collector."""
        self._enabled = PROMETHEUS_AVAILABLE

    @property
    def enabled(self) -> bool:
        """Check if metrics are enabled."""
        return self._enabled

    def set_daemon_info(self, version: str) -> None:
        """Set daemon info gauge.

        Args:
            version: Daemon version string.
        """
        if not self._enabled:
            return
        DAEMON_INFO.labels(version=version).set(1)
        DAEMON_UP.set(1)

    def set_daemon_down(self) -> None:
        """Mark daemon as down."""
        if not self._enabled:
            return
        DAEMON_UP.set(0)

    def record_task_start(self, task_type: str) -> float:
        """Record task processing start.

        Args:
            task_type: Type of task being processed.

        Returns:
            Start timestamp for duration calculation.
        """
        return time.monotonic()

    def record_task_complete(
        self,
        task_type: str,
        start_time: float,
        success: bool = True,
    ) -> None:
        """Record task processing completion.

        Args:
            task_type: Type of task processed.
            start_time: Task start timestamp.
            success: Whether task completed successfully.
        """
        if not self._enabled:
            return

        duration = time.monotonic() - start_time
        status = "success" if success else "error"

        TASKS_PROCESSED.labels(task_type=task_type, status=status).inc()
        TASK_DURATION.labels(task_type=task_type).observe(duration)

    def record_llm_usage(
        self,
        model: str,
        tokens_input: int,
        tokens_output: int,
        cost_usd: float,
        success: bool = True,
    ) -> None:
        """Record LLM API usage.

        Args:
            model: Model name.
            tokens_input: Input tokens consumed.
            tokens_output: Output tokens generated.
            cost_usd: Estimated cost in USD.
            success: Whether the request succeeded.
        """
        if not self._enabled:
            return

        LLM_TOKENS_USED.labels(model=model, direction="input").inc(tokens_input)
        LLM_TOKENS_USED.labels(model=model, direction="output").inc(tokens_output)
        LLM_COST_USD.labels(model=model).inc(cost_usd)

        status = "success" if success else "error"
        LLM_REQUESTS.labels(model=model, status=status).inc()

    def record_decision(self, task_type: str, decision: str) -> None:
        """Record a curation decision.

        Args:
            task_type: Type of task that made the decision.
            decision: Decision made (promote, reject, merge, etc.).
        """
        if not self._enabled:
            return
        DECISIONS.labels(task_type=task_type, decision=decision).inc()

    async def update_queue_metrics(self, task_queue: "TaskQueue") -> None:
        """Update queue depth metrics from task queue.

        Args:
            task_queue: Task queue instance.
        """
        if not self._enabled:
            return

        try:
            stats = await task_queue.get_stats()
            queue_stats = stats.get("queue", {})

            for status in ("pending", "processing", "completed", "failed"):
                count = queue_stats.get(status, 0)
                QUEUE_DEPTH.labels(status=status).set(count)

            dlq_count = stats.get("dead_letter_queue", 0)
            DLQ_DEPTH.set(dlq_count)

        except Exception as e:
            logger.warning(f"Failed to update queue metrics: {e}")

    def generate_metrics(self) -> bytes:
        """Generate Prometheus metrics output.

        Returns:
            Prometheus text format metrics.
        """
        if not self._enabled:
            return b"# Prometheus metrics disabled\n"
        return generate_latest()  # type: ignore[no-any-return]

    def get_content_type(self) -> str:
        """Get content type for metrics endpoint.

        Returns:
            MIME content type.
        """
        if not self._enabled:
            return "text/plain"
        return CONTENT_TYPE_LATEST  # type: ignore[no-any-return]


@asynccontextmanager
async def track_task(
    metrics: MetricsCollector,
    task_type: str,
) -> AsyncGenerator[None, None]:
    """Context manager to track task execution metrics.

    Args:
        metrics: Metrics collector instance.
        task_type: Type of task being tracked.

    Yields:
        None
    """
    start_time = metrics.record_task_start(task_type)
    success = True
    try:
        yield
    except Exception:
        success = False
        raise
    finally:
        metrics.record_task_complete(task_type, start_time, success)


# Global metrics collector instance
_metrics: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    """Get the global metrics collector.

    Returns:
        Metrics collector instance.
    """
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics


def reset_metrics() -> None:
    """Reset metrics collector (for testing)."""
    global _metrics
    _metrics = None
