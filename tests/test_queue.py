"""Tests for task queue management."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from knowledge_curator.config import Settings
from knowledge_curator.core.models import (
    QueuedTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from knowledge_curator.core.queue import TaskQueue
from knowledge_curator.database.repository import Repository


@pytest.fixture
async def task_queue(tmp_path: Path) -> TaskQueue:
    """Create a task queue with test database."""
    db_path = tmp_path / "test_queue.db"
    repo = Repository(db_path)
    await repo.connect()

    settings = Settings(
        db_path=db_path,
        log_level="DEBUG",
    )

    queue = TaskQueue(repo, settings)
    yield queue
    await repo.close()


class TestTaskQueue:
    """Tests for TaskQueue class."""

    @pytest.mark.asyncio
    async def test_enqueue_creates_task(self, task_queue: TaskQueue) -> None:
        """Test that enqueue creates a new task."""
        task_id = await task_queue.enqueue(
            task_type=TaskType.REVIEW_STAGED,
            payload={"entry_id": "test-entry"},
            priority=TaskPriority.HIGH,
        )

        assert task_id is not None
        assert len(task_id) == 36  # UUID format

        task = await task_queue.repo.get_task(task_id)
        assert task is not None
        assert task.task_type == TaskType.REVIEW_STAGED
        assert task.priority == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_process_next_with_handler(self, task_queue: TaskQueue) -> None:
        """Test processing a task with registered handler."""
        handler = AsyncMock()
        task_queue.register_handler(TaskType.REVIEW_STAGED, handler)

        task_id = await task_queue.enqueue(
            task_type=TaskType.REVIEW_STAGED,
            payload={"test": "data"},
        )

        processed = await task_queue.process_next()

        assert processed is True
        handler.assert_called_once()

        # Task should be completed
        task = await task_queue.repo.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_process_next_empty_queue(self, task_queue: TaskQueue) -> None:
        """Test process_next returns False for empty queue."""
        processed = await task_queue.process_next()
        assert processed is False

    @pytest.mark.asyncio
    async def test_handler_failure_triggers_retry(self, task_queue: TaskQueue) -> None:
        """Test that handler failure triggers retry."""
        handler = AsyncMock(side_effect=ValueError("Test error"))
        task_queue.register_handler(TaskType.REVIEW_STAGED, handler)

        task_id = await task_queue.enqueue(
            task_type=TaskType.REVIEW_STAGED,
            payload={},
        )

        await task_queue.process_next()

        # Task should be back to pending with retry count
        task = await task_queue.repo.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.PENDING
        assert task.retry_count == 1

    @pytest.mark.asyncio
    async def test_max_retries_moves_to_dlq(self, task_queue: TaskQueue) -> None:
        """Test that exceeding max retries moves task to DLQ."""
        handler = AsyncMock(side_effect=ValueError("Persistent error"))
        task_queue.register_handler(TaskType.REVIEW_STAGED, handler)

        task_id = await task_queue.enqueue(
            task_type=TaskType.REVIEW_STAGED,
            payload={},
        )

        # Process until max retries exceeded
        for _ in range(4):  # max_attempts = 3, so 4th try should DLQ
            await task_queue.process_next()

        # Task should be in DLQ
        task = await task_queue.repo.get_task(task_id)
        assert task is None  # Removed from main queue

        dlq_count = await task_queue.repo.get_dlq_count()
        assert dlq_count == 1

    @pytest.mark.asyncio
    async def test_get_stats(self, task_queue: TaskQueue) -> None:
        """Test getting queue statistics."""
        await task_queue.enqueue(
            task_type=TaskType.REVIEW_STAGED,
            payload={},
        )
        await task_queue.enqueue(
            task_type=TaskType.PROCESS_FEEDBACK,
            payload={},
        )

        stats = await task_queue.get_stats()

        assert "queue" in stats
        assert stats["queue"].get("pending", 0) == 2
        assert stats["running"] is False

    @pytest.mark.asyncio
    async def test_get_pending_count(self, task_queue: TaskQueue) -> None:
        """Test getting pending task count."""
        assert await task_queue.get_pending_count() == 0

        await task_queue.enqueue(TaskType.REVIEW_STAGED, {})
        await task_queue.enqueue(TaskType.PROCESS_FEEDBACK, {})

        assert await task_queue.get_pending_count() == 2
