"""Tests for scheduler module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge_curator.core.models import TaskPriority, TaskType
from knowledge_curator.scheduler.scheduler import CuratorScheduler


@pytest.fixture
def mock_task_queue() -> MagicMock:
    """Create mock task queue."""
    queue = MagicMock()
    queue.enqueue = AsyncMock(return_value="task-123")
    return queue


@pytest.fixture
def mock_settings() -> MagicMock:
    """Create mock settings with schedule configuration."""
    settings = MagicMock()
    settings.schedules = MagicMock()
    settings.schedules.deduplication = "0 * * * *"
    settings.schedules.obsolescence = "0 3 * * *"
    settings.schedules.gap_analysis = "0 5 * * 0"
    return settings


class TestCuratorScheduler:
    """Tests for CuratorScheduler class."""

    def test_init_creates_scheduler_not_started(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Scheduler should not be started on init."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        assert scheduler.task_queue == mock_task_queue
        assert scheduler.settings == mock_settings
        assert not scheduler.is_running
        assert scheduler._scheduler is None

    @pytest.mark.asyncio
    async def test_start_registers_three_jobs(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Start should register all three scheduled jobs."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        with patch(
            "knowledge_curator.scheduler.scheduler.AsyncIOScheduler"
        ) as mock_aps:
            mock_instance = MagicMock()
            mock_aps.return_value = mock_instance

            await scheduler.start()

            # Should add 3 jobs
            assert mock_instance.add_job.call_count == 3
            mock_instance.start.assert_called_once()
            assert scheduler.is_running

    @pytest.mark.asyncio
    async def test_start_twice_logs_warning(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Starting twice should log warning and not restart."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        with patch(
            "knowledge_curator.scheduler.scheduler.AsyncIOScheduler"
        ) as mock_aps:
            mock_instance = MagicMock()
            mock_aps.return_value = mock_instance

            await scheduler.start()
            await scheduler.start()  # Second call

            # Should only start once
            assert mock_instance.start.call_count == 1

    @pytest.mark.asyncio
    async def test_enqueue_dedup_creates_task(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Dedup job should enqueue deduplicate task."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        await scheduler._enqueue_dedup()

        mock_task_queue.enqueue.assert_called_once_with(
            task_type=TaskType.DEDUPLICATE,
            payload={"triggered_by": "scheduler"},
            priority=TaskPriority.LOW,
        )

    @pytest.mark.asyncio
    async def test_enqueue_obsolescence_creates_task(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Obsolescence job should enqueue detect_obsolescence task."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        await scheduler._enqueue_obsolescence()

        mock_task_queue.enqueue.assert_called_once_with(
            task_type=TaskType.DETECT_OBSOLESCENCE,
            payload={"triggered_by": "scheduler"},
            priority=TaskPriority.LOW,
        )

    @pytest.mark.asyncio
    async def test_enqueue_gap_analysis_creates_task(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Gap analysis job should enqueue identify_gaps task."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        await scheduler._enqueue_gap_analysis()

        mock_task_queue.enqueue.assert_called_once_with(
            task_type=TaskType.IDENTIFY_GAPS,
            payload={"triggered_by": "scheduler"},
            priority=TaskPriority.LOW,
        )

    @pytest.mark.asyncio
    async def test_enqueue_handles_error_gracefully(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Enqueue should handle errors without raising."""
        mock_task_queue.enqueue = AsyncMock(side_effect=Exception("Queue error"))
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        # Should not raise
        await scheduler._enqueue_dedup()

    @pytest.mark.asyncio
    async def test_stop_shuts_down_scheduler(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Stop should shut down the scheduler gracefully."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        with patch(
            "knowledge_curator.scheduler.scheduler.AsyncIOScheduler"
        ) as mock_aps:
            mock_instance = MagicMock()
            mock_aps.return_value = mock_instance

            await scheduler.start()
            assert scheduler.is_running

            await scheduler.stop()

            mock_instance.shutdown.assert_called_once_with(wait=True)
            assert not scheduler.is_running

    @pytest.mark.asyncio
    async def test_stop_when_not_started_is_noop(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Stop on unstarted scheduler should be safe."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        # Should not raise
        await scheduler.stop()
        assert not scheduler.is_running

    def test_get_next_run_times_when_not_started(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """get_next_run_times should return empty dict when not started."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        result = scheduler.get_next_run_times()

        assert result == {}

    @pytest.mark.asyncio
    async def test_get_next_run_times_returns_job_times(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """get_next_run_times should return times for all jobs."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        with patch(
            "knowledge_curator.scheduler.scheduler.AsyncIOScheduler"
        ) as mock_aps:
            mock_job = MagicMock()
            mock_job.id = "test_job"
            mock_job.next_run_time = MagicMock()
            mock_job.next_run_time.isoformat.return_value = "2026-01-05T10:00:00"

            mock_instance = MagicMock()
            mock_instance.get_jobs.return_value = [mock_job]
            mock_aps.return_value = mock_instance

            await scheduler.start()
            result = scheduler.get_next_run_times()

            assert "test_job" in result
            assert result["test_job"] == "2026-01-05T10:00:00"

    @pytest.mark.asyncio
    async def test_get_job_info_returns_job_details(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """get_job_info should return details for all jobs."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        with patch(
            "knowledge_curator.scheduler.scheduler.AsyncIOScheduler"
        ) as mock_aps:
            mock_job = MagicMock()
            mock_job.id = "deduplicate"
            mock_job.name = "Deduplicate knowledge entries"
            mock_job.next_run_time = MagicMock()
            mock_job.next_run_time.isoformat.return_value = "2026-01-05T10:00:00"

            mock_instance = MagicMock()
            mock_instance.get_jobs.return_value = [mock_job]
            mock_aps.return_value = mock_instance

            await scheduler.start()
            result = scheduler.get_job_info()

            assert len(result) == 1
            assert result[0]["id"] == "deduplicate"
            assert result[0]["name"] == "Deduplicate knowledge entries"
            assert result[0]["next_run"] == "2026-01-05T10:00:00"

    def test_get_job_info_when_not_started(
        self,
        mock_task_queue: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """get_job_info should return empty list when not started."""
        scheduler = CuratorScheduler(mock_task_queue, mock_settings)

        result = scheduler.get_job_info()

        assert result == []
