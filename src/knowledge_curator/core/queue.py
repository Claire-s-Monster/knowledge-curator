"""Task queue management for Knowledge Curator.

Provides a high-level interface for task queue operations with:
- Priority-based task scheduling
- Retry handling with exponential backoff
- Dead letter queue for failed tasks
- Queue statistics and monitoring
"""

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from knowledge_curator.config import Settings
from knowledge_curator.core.models import (
    QueuedTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from knowledge_curator.database.repository import Repository
from knowledge_curator.logging import clear_task_context, set_task_context

# Type alias for task handlers
TaskHandler = Callable[[QueuedTask], Coroutine[Any, Any, None]]


class TaskQueue:
    """High-level task queue manager.

    Wraps the repository with business logic for:
    - Task creation and enqueueing
    - Task processing with handlers
    - Retry and failure handling
    - Statistics and monitoring
    - Graceful shutdown with task completion
    """

    def __init__(self, repository: Repository, settings: Settings) -> None:
        """Initialize task queue.

        Args:
            repository: Database repository instance.
            settings: Application settings.
        """
        self.repo = repository
        self.settings = settings
        self._handlers: dict[TaskType, TaskHandler] = {}
        self._running = False
        self._current_task: QueuedTask | None = None
        self._task_completed_event = asyncio.Event()
        self._task_completed_event.set()  # Initially no task running

    def register_handler(self, task_type: TaskType, handler: TaskHandler) -> None:
        """Register a handler for a task type.

        Args:
            task_type: Type of task to handle.
            handler: Async function to process tasks of this type.
        """
        self._handlers[task_type] = handler
        logger.debug(f"Registered handler for {task_type.value}")

    async def enqueue(
        self,
        task_type: TaskType,
        payload: dict[str, Any],
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> str:
        """Create and enqueue a new task.

        Args:
            task_type: Type of task.
            payload: Task-specific data.
            priority: Task priority level.

        Returns:
            Task ID.
        """
        task_id = str(uuid.uuid4())

        task = QueuedTask(
            id=task_id,
            task_type=task_type,
            priority=priority,
            payload=payload,
            status=TaskStatus.PENDING,
            created_at=datetime.now(UTC),
        )

        await self.repo.enqueue_task(task)
        logger.info(
            f"Enqueued task {task_id[:8]}... type={task_type.value} priority={priority.name}"
        )

        return task_id

    async def process_next(self) -> bool:
        """Process the next task in the queue.

        Returns:
            True if a task was processed, False if queue was empty.
        """
        task = await self.repo.get_next_task()

        if task is None:
            return False

        # Signal that a task is being processed
        self._task_completed_event.clear()
        self._current_task = task

        # Set task context for structured logging
        set_task_context(task_id=task.id, task_type=task.task_type.value)

        handler = self._handlers.get(task.task_type)
        if handler is None:
            logger.error(f"No handler for task type: {task.task_type.value}")
            await self._handle_failure(task, f"No handler for {task.task_type.value}")
            self._current_task = None
            clear_task_context()
            self._task_completed_event.set()
            return True

        # Mark as processing
        await self.repo.update_task_status(task.id, TaskStatus.PROCESSING)
        logger.info(f"Processing task {task.id[:8]}... type={task.task_type.value}")

        try:
            await handler(task)
            await self.repo.update_task_status(task.id, TaskStatus.COMPLETED)
            logger.info(f"Completed task {task.id[:8]}...")

        except Exception as e:
            logger.exception(f"Task {task.id[:8]}... failed: {e}")
            await self._handle_failure(task, str(e))

        self._current_task = None
        clear_task_context()
        self._task_completed_event.set()
        return True

    async def _handle_failure(self, task: QueuedTask, error: str) -> None:
        """Handle a failed task with retry or DLQ.

        Args:
            task: Failed task.
            error: Error message.
        """
        new_retry_count = await self.repo.increment_retry(task.id)

        if new_retry_count >= self.settings.retry.max_attempts:
            logger.warning(f"Task {task.id[:8]}... exceeded retries, moving to DLQ")
            await self.repo.move_to_dlq(task, error)
        else:
            # Calculate backoff delay
            delay = self.settings.retry.backoff_base_seconds * (
                self.settings.retry.backoff_multiplier ** (new_retry_count - 1)
            )
            logger.info(
                f"Task {task.id[:8]}... retry {new_retry_count}/{self.settings.retry.max_attempts} "
                f"in {delay}s"
            )
            # Task is already set back to pending by increment_retry

    async def run(self, poll_interval: float = 1.0) -> None:
        """Run the queue processor loop.

        Args:
            poll_interval: Seconds to wait when queue is empty.
        """
        self._running = True
        logger.info("Task queue processor started")

        while self._running:
            try:
                processed = await self.process_next()
                if not processed:
                    # Queue is empty, wait before checking again
                    await asyncio.sleep(poll_interval)
            except Exception as e:
                logger.exception(f"Queue processor error: {e}")
                await asyncio.sleep(poll_interval)

        logger.info("Task queue processor stopped")

    def stop(self) -> None:
        """Signal the queue processor to stop."""
        self._running = False
        logger.info("Task queue processor stopping...")

    @property
    def is_processing(self) -> bool:
        """Check if a task is currently being processed.

        Returns:
            True if a task is in progress.
        """
        return self._current_task is not None

    @property
    def current_task_id(self) -> str | None:
        """Get the ID of the currently processing task.

        Returns:
            Task ID or None if no task is processing.
        """
        return self._current_task.id if self._current_task else None

    async def wait_for_current_task(self, timeout: float | None = None) -> bool:
        """Wait for the current task to complete.

        Args:
            timeout: Maximum seconds to wait, None for infinite.

        Returns:
            True if task completed, False if timeout occurred.
        """
        if not self.is_processing:
            return True

        task_id = self._current_task.id if self._current_task else "unknown"
        logger.info(f"Waiting for current task {task_id[:8]}... to complete")

        try:
            await asyncio.wait_for(
                self._task_completed_event.wait(),
                timeout=timeout,
            )
            logger.info(f"Task {task_id[:8]}... completed")
            return True
        except TimeoutError:
            logger.warning(
                f"Timeout waiting for task {task_id[:8]}... after {timeout}s"
            )
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Get queue statistics.

        Returns:
            Dictionary with queue stats.
        """
        queue_stats = await self.repo.get_queue_stats()
        dlq_count = await self.repo.get_dlq_count()

        return {
            "queue": queue_stats,
            "dead_letter_queue": dlq_count,
            "handlers_registered": list(self._handlers.keys()),
            "running": self._running,
            "current_task": self._current_task.id if self._current_task else None,
        }

    async def get_pending_count(self) -> int:
        """Get count of pending tasks.

        Returns:
            Number of pending tasks.
        """
        return await self.repo.get_pending_count()
