"""Tests for database repository."""

import pytest

from knowledge_curator.core.models import (
    QueuedTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from knowledge_curator.database.repository import Repository


class TestRepository:
    """Tests for Repository class."""

    @pytest.mark.asyncio
    async def test_enqueue_and_get_task(self, repository: Repository) -> None:
        """Test enqueueing and retrieving a task."""
        task = QueuedTask(
            id="test-task-1",
            task_type=TaskType.REVIEW_STAGED,
            priority=TaskPriority.HIGH,
            payload={"entry_id": "entry-123"},
        )

        await repository.enqueue_task(task)
        retrieved = await repository.get_task("test-task-1")

        assert retrieved is not None
        assert retrieved.id == "test-task-1"
        assert retrieved.task_type == TaskType.REVIEW_STAGED
        assert retrieved.priority == TaskPriority.HIGH
        assert retrieved.payload == {"entry_id": "entry-123"}
        assert retrieved.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_next_task_priority_order(self, repository: Repository) -> None:
        """Test that get_next_task respects priority."""
        # Enqueue tasks in reverse priority order
        low_task = QueuedTask(
            id="low-task",
            task_type=TaskType.DEDUPLICATE,
            priority=TaskPriority.LOW,
            payload={},
        )
        high_task = QueuedTask(
            id="high-task",
            task_type=TaskType.REVIEW_STAGED,
            priority=TaskPriority.HIGH,
            payload={},
        )
        critical_task = QueuedTask(
            id="critical-task",
            task_type=TaskType.MANUAL_REVIEW,
            priority=TaskPriority.CRITICAL,
            payload={},
        )

        await repository.enqueue_task(low_task)
        await repository.enqueue_task(high_task)
        await repository.enqueue_task(critical_task)

        # Should get critical first
        next_task = await repository.get_next_task()
        assert next_task is not None
        assert next_task.id == "critical-task"

    @pytest.mark.asyncio
    async def test_update_task_status(self, repository: Repository) -> None:
        """Test updating task status."""
        task = QueuedTask(
            id="status-test",
            task_type=TaskType.PROCESS_FEEDBACK,
            payload={},
        )
        await repository.enqueue_task(task)

        # Update to processing
        await repository.update_task_status("status-test", TaskStatus.PROCESSING)
        updated = await repository.get_task("status-test")
        assert updated is not None
        assert updated.status == TaskStatus.PROCESSING
        assert updated.started_at is not None

        # Update to completed
        await repository.update_task_status("status-test", TaskStatus.COMPLETED)
        completed = await repository.get_task("status-test")
        assert completed is not None
        assert completed.status == TaskStatus.COMPLETED
        assert completed.completed_at is not None

    @pytest.mark.asyncio
    async def test_retry_increment(self, repository: Repository) -> None:
        """Test retry count increment."""
        task = QueuedTask(
            id="retry-test",
            task_type=TaskType.REVIEW_STAGED,
            payload={},
        )
        await repository.enqueue_task(task)

        count1 = await repository.increment_retry("retry-test")
        assert count1 == 1

        count2 = await repository.increment_retry("retry-test")
        assert count2 == 2

        # Status should be reset to pending
        updated = await repository.get_task("retry-test")
        assert updated is not None
        assert updated.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_move_to_dlq(self, repository: Repository) -> None:
        """Test moving task to dead letter queue."""
        task = QueuedTask(
            id="dlq-test",
            task_type=TaskType.REVIEW_STAGED,
            payload={"test": "data"},
        )
        await repository.enqueue_task(task)

        await repository.move_to_dlq(task, "Test error message")

        # Task should be removed from main queue
        original = await repository.get_task("dlq-test")
        assert original is None

        # DLQ count should increase
        dlq_count = await repository.get_dlq_count()
        assert dlq_count == 1

    @pytest.mark.asyncio
    async def test_queue_stats(self, repository: Repository) -> None:
        """Test queue statistics."""
        # Enqueue some tasks
        for i in range(3):
            task = QueuedTask(
                id=f"stats-test-{i}",
                task_type=TaskType.REVIEW_STAGED,
                payload={},
            )
            await repository.enqueue_task(task)

        # Complete one
        await repository.update_task_status("stats-test-0", TaskStatus.COMPLETED)

        stats = await repository.get_queue_stats()

        assert stats.get("pending", 0) == 2
        assert stats.get("completed", 0) == 1

    @pytest.mark.asyncio
    async def test_decision_logging(self, repository: Repository) -> None:
        """Test decision logging."""
        await repository.log_decision(
            task_id="task-123",
            entry_id="entry-456",
            decision="promote",
            confidence=0.95,
            reason="High quality entry",
            model_used="claude-sonnet-4",
            tokens_input=1000,
            tokens_output=200,
        )

        # Verify by checking it doesn't raise
        # Full verification would require a get_decisions method

    @pytest.mark.asyncio
    async def test_cost_tracking(self, repository: Repository) -> None:
        """Test cost tracking."""
        await repository.record_cost(
            date="2026-01-04",
            model="claude-sonnet-4",
            tokens_input=1000,
            tokens_output=200,
            estimated_cost_usd=0.05,
        )

        cost = await repository.get_daily_cost("2026-01-04")
        assert cost == pytest.approx(0.05)

        # Add more cost
        await repository.record_cost(
            date="2026-01-04",
            model="claude-sonnet-4",
            tokens_input=500,
            tokens_output=100,
            estimated_cost_usd=0.02,
        )

        updated_cost = await repository.get_daily_cost("2026-01-04")
        assert updated_cost == pytest.approx(0.07)
