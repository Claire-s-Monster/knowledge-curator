"""Main daemon loop for Knowledge Curator.

Orchestrates:
- Task queue processing
- Webhook server (FastAPI + uvicorn)
- Scheduled tasks (via scheduler module, Phase 6)
- Graceful shutdown handling
"""

import asyncio
import signal
import time
from pathlib import Path

import uvicorn
from loguru import logger

from knowledge_curator.clients import KnowledgeBridgeClient, KnowledgeStoreClient
from knowledge_curator.config import Settings, get_settings
from knowledge_curator.core.models import QueuedTask, TaskType
from knowledge_curator.core.queue import TaskQueue
from knowledge_curator.database.repository import Repository
from knowledge_curator.llm.client import HAS_SDK, CuratorLLMClient
from knowledge_curator.logging import configure_logging
from knowledge_curator.metrics import get_metrics
from knowledge_curator.scheduler import CuratorScheduler
from knowledge_curator.tasks import (
    DedupContext,
    DedupPayload,
    FeedbackContext,
    FeedbackPayload,
    GapContext,
    GapPayload,
    ObsolescenceContext,
    ObsolescencePayload,
    ReviewContext,
    ReviewPayload,
    deduplicate,
    detect_obsolescence,
    identify_gaps,
    process_feedback,
    review_staged_entry,
)
from knowledge_curator.webhooks.server import WebhookServer


class CuratorDaemon:
    """Main daemon orchestrator.

    Manages the lifecycle of:
    - Database connection
    - Task queue processor
    - Webhook server (FastAPI)
    - LLM client with rate limiting
    - Scheduler for batch operations
    - Graceful shutdown with task completion
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize daemon with settings.

        Args:
            settings: Application settings.
        """
        self.settings = settings
        self.repository: Repository | None = None
        self.task_queue: TaskQueue | None = None
        self.llm_client: CuratorLLMClient | None = None
        self.knowledge_store_client: KnowledgeStoreClient | None = None
        self.bridge_client: KnowledgeBridgeClient | None = None
        self.webhook_server: WebhookServer | None = None
        self.scheduler: CuratorScheduler | None = None
        self._uvicorn_server: uvicorn.Server | None = None
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._is_shutting_down = False
        self._shutdown_start_time: float | None = None

    async def start(self) -> None:
        """Start the daemon and all its components."""
        logger.info("Starting Knowledge Curator daemon...")

        # Initialize database
        self.repository = Repository(self.settings.db_path)
        await self.repository.connect()
        logger.info(f"Connected to database: {self.settings.db_path}")

        # Refuse to start if the Claude Agent SDK is not installed - the
        # daemon's only job is LLM curation, and booting without the SDK
        # previously hid a total outage behind a green /health endpoint.
        if not HAS_SDK:
            logger.critical(
                "claude-agent-sdk is not installed - the daemon cannot "
                "perform LLM curation, its only job. Install the "
                "'claude-agent-sdk' package (see pyproject.toml "
                "[tool.pixi.pypi-dependencies]) and restart."
            )
            raise RuntimeError("claude-agent-sdk is not installed; refusing to start")

        # Initialize LLM client (SDK uses Claude CLI credentials if no API key)
        self.llm_client = CuratorLLMClient(self.settings, repository=self.repository)
        await self.llm_client.load_daily_cost()
        logger.info(
            f"LLM client initialized: budget=${self.settings.rate_limits.daily_budget_usd}/day"
        )

        # Initialize external service clients
        self.knowledge_store_client = KnowledgeStoreClient.from_settings(self.settings)
        self.bridge_client = KnowledgeBridgeClient.from_settings(self.settings)
        logger.info(f"Knowledge store client: {self.settings.knowledge_store_url}")
        logger.info(f"Bridge client: {self.settings.knowledge_bridge_url}")

        # Initialize task queue
        self.task_queue = TaskQueue(self.repository, self.settings)
        self._register_task_handlers()

        # Start background tasks
        self._tasks.append(asyncio.create_task(self._run_queue_processor()))

        # Start webhook server
        self._tasks.append(asyncio.create_task(self._run_webhook_server()))

        # Phase 6: Start scheduler
        if self.llm_client and self.task_queue:
            self.scheduler = CuratorScheduler(self.task_queue, self.settings)
            await self.scheduler.start()
            logger.info("Scheduler started with scheduled jobs")
        else:
            logger.warning(
                "Scheduler not started - LLM client or task queue unavailable"
            )

        # Initialize metrics
        from knowledge_curator import __version__

        metrics = get_metrics()
        metrics.set_daemon_info(__version__)
        logger.info(f"  Metrics: {'enabled' if metrics.enabled else 'disabled'}")

        logger.info("Knowledge Curator daemon started")
        logger.info(f"  Database: {self.settings.db_path}")
        logger.info(
            f"  Webhook: http://{self.settings.webhook_host}:{self.settings.webhook_port}"
        )
        logger.info(f"  LLM: {'enabled' if self.llm_client else 'disabled'}")
        logger.info(f"  Scheduler: {'enabled' if self.scheduler else 'disabled'}")
        logger.info(f"  Log level: {self.settings.log_level}")

    def _register_task_handlers(self) -> None:
        """Register task handlers with the queue."""
        if self.task_queue is None:
            return

        # Build review context for handlers
        if self.llm_client is None:
            logger.warning("LLM client not available - some handlers may fail")

        # Phase 4: Register review_staged_entry handler
        review_context = ReviewContext(
            settings=self.settings,
            llm_client=self.llm_client,  # type: ignore[arg-type]
            knowledge_store_client=self.knowledge_store_client,  # type: ignore[arg-type]
            bridge_client=self.bridge_client,  # type: ignore[arg-type]
            repository=self.repository,
        )

        async def handle_review(task: QueuedTask) -> None:
            """Handle review_staged_entry task."""
            payload = ReviewPayload(**task.payload)
            await review_staged_entry(payload, review_context, task_id=task.id)

        self.task_queue.register_handler(TaskType.REVIEW_STAGED, handle_review)
        logger.info("Registered handler: review_staged_entry")

        # Phase 5: Register process_feedback handler
        feedback_context = FeedbackContext(
            settings=self.settings,
            knowledge_store_client=self.knowledge_store_client,  # type: ignore[arg-type]
            bridge_client=self.bridge_client,  # type: ignore[arg-type]
        )

        async def handle_feedback(task: QueuedTask) -> None:
            """Handle process_feedback task."""
            payload = FeedbackPayload(**task.payload)
            await process_feedback(payload, feedback_context)

        self.task_queue.register_handler(TaskType.PROCESS_FEEDBACK, handle_feedback)
        logger.info("Registered handler: process_feedback")

        # Phase 6: Register scheduled task handlers

        # Deduplication handler
        dedup_context = DedupContext(
            settings=self.settings,
            llm_client=self.llm_client,  # type: ignore[arg-type]
            knowledge_store_client=self.knowledge_store_client,  # type: ignore[arg-type]
        )

        async def handle_dedup(task: QueuedTask) -> None:
            """Handle deduplicate task."""
            payload = DedupPayload(**task.payload)
            await deduplicate(payload, dedup_context)

        self.task_queue.register_handler(TaskType.DEDUPLICATE, handle_dedup)
        logger.info("Registered handler: deduplicate")

        # Obsolescence detection handler
        obsolescence_context = ObsolescenceContext(
            settings=self.settings,
            llm_client=self.llm_client,  # type: ignore[arg-type]
            knowledge_store_client=self.knowledge_store_client,  # type: ignore[arg-type]
        )

        async def handle_obsolescence(task: QueuedTask) -> None:
            """Handle detect_obsolescence task."""
            payload = ObsolescencePayload(**task.payload)
            await detect_obsolescence(payload, obsolescence_context)

        self.task_queue.register_handler(
            TaskType.DETECT_OBSOLESCENCE, handle_obsolescence
        )
        logger.info("Registered handler: detect_obsolescence")

        # Gap analysis handler
        gap_context = GapContext(
            settings=self.settings,
            llm_client=self.llm_client,  # type: ignore[arg-type]
            knowledge_store_client=self.knowledge_store_client,  # type: ignore[arg-type]
            bridge_client=self.bridge_client,  # type: ignore[arg-type]
        )

        async def handle_gaps(task: QueuedTask) -> None:
            """Handle identify_gaps task."""
            payload = GapPayload(**task.payload)
            await identify_gaps(payload, gap_context)

        self.task_queue.register_handler(TaskType.IDENTIFY_GAPS, handle_gaps)
        logger.info("Registered handler: identify_gaps")

        # Placeholder for manual review (not yet implemented)
        async def placeholder_handler(task: QueuedTask) -> None:
            logger.warning(
                f"Placeholder handler for {task.task_type.value} - not implemented"
            )
            raise NotImplementedError(
                f"Handler for {task.task_type.value} not implemented"
            )

        # Only MANUAL_REVIEW is unimplemented now
        if TaskType.MANUAL_REVIEW not in [
            TaskType.REVIEW_STAGED,
            TaskType.PROCESS_FEEDBACK,
            TaskType.DEDUPLICATE,
            TaskType.DETECT_OBSOLESCENCE,
            TaskType.IDENTIFY_GAPS,
        ]:
            self.task_queue.register_handler(
                TaskType.MANUAL_REVIEW, placeholder_handler
            )

    async def _run_queue_processor(self) -> None:
        """Run the task queue processor until shutdown."""
        if self.task_queue is None:
            return

        try:
            await self.task_queue.run(poll_interval=1.0)
        except asyncio.CancelledError:
            logger.info("Queue processor cancelled")
        except Exception as e:
            logger.exception(f"Queue processor error: {e}")

    async def _run_webhook_server(self) -> None:
        """Run the webhook server until shutdown."""
        self.webhook_server = WebhookServer(self)

        config = uvicorn.Config(
            app=self.webhook_server.app,
            host=self.settings.webhook_host,
            port=self.settings.webhook_port,
            log_level="warning",  # Reduce uvicorn noise
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)

        try:
            logger.info(
                f"Webhook server listening on "
                f"http://{self.settings.webhook_host}:{self.settings.webhook_port}"
            )
            await self._uvicorn_server.serve()
        except asyncio.CancelledError:
            logger.info("Webhook server cancelled")
        except Exception as e:
            logger.exception(f"Webhook server error: {e}")

    @property
    def is_shutting_down(self) -> bool:
        """Check if daemon is in shutdown state."""
        return self._is_shutting_down

    async def stop(self, force: bool = False) -> None:
        """Stop the daemon gracefully.

        Args:
            force: If True, skip waiting for current task and force shutdown.
        """
        # Prevent double shutdown
        if self._is_shutting_down:
            logger.warning("Shutdown already in progress")
            return

        self._is_shutting_down = True
        self._shutdown_start_time = time.monotonic()
        timeout = self.settings.shutdown.timeout_seconds

        logger.info(
            f"Stopping Knowledge Curator daemon (timeout={timeout}s, force={force})..."
        )

        # Phase 1: Stop accepting new work
        # Stop scheduler first (no new scheduled tasks)
        if self.scheduler:
            await self.scheduler.stop()
            logger.info("Scheduler stopped")

        # Signal webhook server to stop (no new webhooks)
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            logger.info("Webhook server signaled to stop")

        # Phase 2: Wait for current task to complete (unless force)
        if self.task_queue and not force and self.task_queue.is_processing:
            task_id = self.task_queue.current_task_id or "unknown"
            logger.info(f"Waiting for current task {task_id[:8]}... to complete")

            # Calculate remaining timeout
            elapsed = time.monotonic() - self._shutdown_start_time
            remaining_timeout = max(1.0, timeout - elapsed)

            task_completed = await self.task_queue.wait_for_current_task(
                timeout=remaining_timeout
            )

            if not task_completed:
                logger.warning(
                    f"Task {task_id[:8]}... did not complete within timeout, "
                    "proceeding with shutdown"
                )

        # Phase 3: Signal queue processor to stop
        if self.task_queue:
            self.task_queue.stop()
            logger.info("Task queue processor stopped")

        # Phase 4: Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        # Wait for tasks to complete with remaining timeout
        if self._tasks:
            elapsed = time.monotonic() - self._shutdown_start_time
            remaining_timeout = max(1.0, timeout - elapsed)

            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=remaining_timeout,
                )
                logger.info("Background tasks completed")
            except TimeoutError:
                logger.warning("Background tasks did not complete within timeout")

        # Phase 5: Close external clients
        close_errors: list[str] = []

        if self.knowledge_store_client:
            try:
                await self.knowledge_store_client.close()
            except Exception as e:
                close_errors.append(f"knowledge_store: {e}")

        if self.bridge_client:
            try:
                await self.bridge_client.close()
            except Exception as e:
                close_errors.append(f"bridge: {e}")

        if close_errors:
            logger.warning(f"Errors closing clients: {close_errors}")
        else:
            logger.info("External clients closed")

        # Phase 6: Close database connection
        if self.repository:
            try:
                await self.repository.close()
                logger.info("Database connection closed")
            except Exception as e:
                logger.error(f"Error closing database: {e}")

        # Calculate shutdown duration
        shutdown_duration = time.monotonic() - self._shutdown_start_time
        self._shutdown_event.set()
        logger.info(
            f"Knowledge Curator daemon stopped (duration={shutdown_duration:.2f}s)"
        )

    async def wait_for_shutdown(self) -> None:
        """Wait until shutdown is complete."""
        await self._shutdown_event.wait()

    async def health_check(self) -> dict[str, object]:
        """Perform health check.

        Returns:
            Health status dictionary.
        """
        # Check shutdown state first
        if self._is_shutting_down:
            return {
                "status": "shutting_down",
                "message": "Daemon is shutting down",
            }

        status: dict[str, object] = {
            "status": "healthy",
            "database": "unknown",
            "queue": {},
            "sdk_available": HAS_SDK,
        }

        # Check database
        try:
            if self.repository:
                await self.repository.get_pending_count()
                status["database"] = "connected"
            else:
                status["database"] = "not initialized"
                status["status"] = "unhealthy"
        except Exception as e:
            status["database"] = f"error: {e}"
            status["status"] = "unhealthy"

        # Check queue
        if self.task_queue:
            try:
                status["queue"] = await self.task_queue.get_stats()
            except Exception as e:
                status["queue"] = {"error": str(e)}
                status["status"] = "unhealthy"

        # Check LLM client
        if self.llm_client:
            try:
                llm_stats = self.llm_client.get_usage_stats()
                llm_status: dict[str, object] = {
                    "enabled": True,
                    "budget_remaining_usd": llm_stats["budget_remaining_usd"],
                    "budget_used_percent": llm_stats["budget_used_percent"],
                    "requests_today": llm_stats["requests"],
                }
                if self.llm_client.is_budget_exceeded():
                    llm_status["warning"] = "Daily budget exceeded"
                status["llm"] = llm_status
            except Exception as e:
                status["llm"] = {"error": str(e)}
        else:
            status["llm"] = {"enabled": False}

        # Check scheduler (Phase 6)
        if self.scheduler:
            status["scheduler"] = {
                "running": self.scheduler.is_running,
                "next_runs": self.scheduler.get_next_run_times(),
                "jobs": self.scheduler.get_job_info(),
            }
        else:
            status["scheduler"] = {"enabled": False}

        return status


def setup_signal_handlers(
    daemon: CuratorDaemon, loop: asyncio.AbstractEventLoop
) -> None:
    """Set up signal handlers for graceful shutdown.

    Handles:
    - First signal: Initiate graceful shutdown
    - Second signal: Force immediate shutdown

    Args:
        daemon: Daemon instance.
        loop: Event loop.
    """
    signal_count = {"value": 0}

    def signal_handler(sig: signal.Signals) -> None:
        signal_count["value"] += 1

        if signal_count["value"] == 1:
            logger.info(f"Received {sig.name}, initiating graceful shutdown...")
            logger.info("Send signal again to force immediate shutdown")
            loop.create_task(daemon.stop(force=False))
        else:
            logger.warning(f"Received {sig.name} again, forcing immediate shutdown!")
            loop.create_task(daemon.stop(force=True))

    def add_handler_for_signal(target_sig: signal.Signals) -> None:
        """Add signal handler with properly captured signal."""
        loop.add_signal_handler(target_sig, lambda: signal_handler(target_sig))

    for sig in (signal.SIGTERM, signal.SIGINT):
        add_handler_for_signal(sig)


async def run_daemon(config_path: Path | None = None) -> None:
    """Run the curator daemon.

    Args:
        config_path: Optional path to YAML config file.
    """
    # Load settings
    settings = get_settings(config_path)

    # Configure structured logging
    configure_logging(settings)

    # Create and start daemon
    daemon = CuratorDaemon(settings)

    # Set up signal handlers
    loop = asyncio.get_running_loop()
    setup_signal_handlers(daemon, loop)

    try:
        await daemon.start()
        await daemon.wait_for_shutdown()
    except Exception as e:
        logger.exception(f"Daemon error: {e}")
        await daemon.stop()
        raise
