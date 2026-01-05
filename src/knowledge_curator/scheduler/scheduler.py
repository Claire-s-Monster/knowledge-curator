"""APScheduler integration for scheduled tasks.

Implements the scheduler component for Phase 6:
- Deduplication (hourly)
- Obsolescence detection (daily 3 AM)
- Gap analysis (weekly Sunday 5 AM)

Jobs enqueue tasks into TaskQueue for persistent, retry-aware execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from knowledge_curator.core.models import TaskPriority, TaskType

if TYPE_CHECKING:
    from knowledge_curator.config import Settings
    from knowledge_curator.core.queue import TaskQueue

logger = logging.getLogger(__name__)


class CuratorScheduler:
    """Manages scheduled batch tasks using APScheduler.

    Responsibilities:
    - Parse cron expressions from settings
    - Register jobs that enqueue tasks into TaskQueue
    - Graceful startup/shutdown
    - Job execution error handling

    Jobs do NOT execute task logic directly - they enqueue tasks into
    the persistent TaskQueue, ensuring retry logic and observability.
    """

    def __init__(
        self,
        task_queue: TaskQueue,
        settings: Settings,
    ) -> None:
        """Initialize the scheduler.

        Args:
            task_queue: Queue for enqueuing scheduled tasks.
            settings: Application settings with schedule configuration.
        """
        self.task_queue = task_queue
        self.settings = settings
        self._scheduler: AsyncIOScheduler | None = None
        self._started = False

    async def start(self) -> None:
        """Start the scheduler and register jobs."""
        if self._started:
            logger.warning("Scheduler already started")
            return

        self._scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={
                "coalesce": True,  # Combine missed runs into single execution
                "max_instances": 1,  # Only one instance per job at a time
                "misfire_grace_time": 3600,  # 1 hour grace for missed jobs
            },
        )

        self._register_jobs()
        self._scheduler.start()
        self._started = True
        logger.info("Scheduler started with jobs: deduplicate, obsolescence, gap_analysis")

    def _register_jobs(self) -> None:
        """Register all scheduled jobs."""
        if self._scheduler is None:
            return

        # Deduplication - hourly
        self._scheduler.add_job(
            self._enqueue_dedup,
            CronTrigger.from_crontab(self.settings.schedules.deduplication),
            id="deduplicate",
            name="Deduplicate knowledge entries",
            replace_existing=True,
        )
        logger.info(f"Registered deduplicate job: {self.settings.schedules.deduplication}")

        # Obsolescence detection - daily at 3 AM
        self._scheduler.add_job(
            self._enqueue_obsolescence,
            CronTrigger.from_crontab(self.settings.schedules.obsolescence),
            id="obsolescence",
            name="Detect obsolete entries",
            replace_existing=True,
        )
        logger.info(f"Registered obsolescence job: {self.settings.schedules.obsolescence}")

        # Gap analysis - weekly Sunday 5 AM
        self._scheduler.add_job(
            self._enqueue_gap_analysis,
            CronTrigger.from_crontab(self.settings.schedules.gap_analysis),
            id="gap_analysis",
            name="Identify knowledge gaps",
            replace_existing=True,
        )
        logger.info(f"Registered gap_analysis job: {self.settings.schedules.gap_analysis}")

    async def _enqueue_dedup(self) -> None:
        """Enqueue deduplication task."""
        logger.info("Scheduler triggering: deduplicate")
        try:
            task_id = await self.task_queue.enqueue(
                task_type=TaskType.DEDUPLICATE,
                payload={"triggered_by": "scheduler"},
                priority=TaskPriority.LOW,
            )
            logger.debug(f"Enqueued deduplicate task: {task_id}")
        except Exception as e:
            logger.error(f"Failed to enqueue deduplicate task: {e}")

    async def _enqueue_obsolescence(self) -> None:
        """Enqueue obsolescence detection task."""
        logger.info("Scheduler triggering: detect_obsolescence")
        try:
            task_id = await self.task_queue.enqueue(
                task_type=TaskType.DETECT_OBSOLESCENCE,
                payload={"triggered_by": "scheduler"},
                priority=TaskPriority.LOW,
            )
            logger.debug(f"Enqueued obsolescence task: {task_id}")
        except Exception as e:
            logger.error(f"Failed to enqueue obsolescence task: {e}")

    async def _enqueue_gap_analysis(self) -> None:
        """Enqueue gap analysis task."""
        logger.info("Scheduler triggering: identify_gaps")
        try:
            task_id = await self.task_queue.enqueue(
                task_type=TaskType.IDENTIFY_GAPS,
                payload={"triggered_by": "scheduler"},
                priority=TaskPriority.LOW,
            )
            logger.debug(f"Enqueued gap_analysis task: {task_id}")
        except Exception as e:
            logger.error(f"Failed to enqueue gap_analysis task: {e}")

    async def stop(self) -> None:
        """Stop the scheduler gracefully."""
        if self._scheduler and self._started:
            self._scheduler.shutdown(wait=True)
            self._started = False
            logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._started

    def get_next_run_times(self) -> dict[str, str | None]:
        """Get next scheduled run times for monitoring.

        Returns:
            Dict mapping job ID to next run time ISO string.
        """
        if not self._scheduler:
            return {}

        result = {}
        for job in self._scheduler.get_jobs():
            next_run = job.next_run_time
            result[job.id] = next_run.isoformat() if next_run else None
        return result

    def get_job_info(self) -> list[dict[str, str | None]]:
        """Get information about all scheduled jobs.

        Returns:
            List of job info dicts with id, name, next_run.
        """
        if not self._scheduler:
            return []

        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return jobs
