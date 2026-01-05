"""Main daemon loop for Knowledge Curator.

Orchestrates:
- Task queue processing
- Webhook server (FastAPI + uvicorn)
- Scheduled tasks (via scheduler module, Phase 6)
- Graceful shutdown handling
"""

import asyncio
import signal
from pathlib import Path

import uvicorn
from loguru import logger

from knowledge_curator.config import Settings, get_settings
from knowledge_curator.core.models import QueuedTask
from knowledge_curator.core.queue import TaskQueue
from knowledge_curator.database.repository import Repository
from knowledge_curator.llm.client import CuratorLLMClient
from knowledge_curator.webhooks.server import WebhookServer


class CuratorDaemon:
    """Main daemon orchestrator.

    Manages the lifecycle of:
    - Database connection
    - Task queue processor
    - Webhook server (FastAPI)
    - LLM client with rate limiting
    - Future: Scheduler
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
        self.webhook_server: WebhookServer | None = None
        self._uvicorn_server: uvicorn.Server | None = None
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Start the daemon and all its components."""
        logger.info("Starting Knowledge Curator daemon...")

        # Initialize database
        self.repository = Repository(self.settings.db_path)
        await self.repository.connect()
        logger.info(f"Connected to database: {self.settings.db_path}")

        # Initialize LLM client
        if self.settings.anthropic_api_key:
            self.llm_client = CuratorLLMClient(self.settings)
            logger.info(
                f"LLM client initialized: budget=${self.settings.rate_limits.daily_budget_usd}/day"
            )
        else:
            logger.warning("No Anthropic API key configured - LLM features disabled")

        # Initialize task queue
        self.task_queue = TaskQueue(self.repository, self.settings)
        self._register_task_handlers()

        # Start background tasks
        self._tasks.append(asyncio.create_task(self._run_queue_processor()))

        # Start webhook server
        self._tasks.append(asyncio.create_task(self._run_webhook_server()))

        # TODO Phase 6: Start scheduler

        logger.info("Knowledge Curator daemon started")
        logger.info(f"  Database: {self.settings.db_path}")
        logger.info(
            f"  Webhook: http://{self.settings.webhook_host}:{self.settings.webhook_port}"
        )
        logger.info(f"  LLM: {'enabled' if self.llm_client else 'disabled'}")
        logger.info(f"  Log level: {self.settings.log_level}")

    def _register_task_handlers(self) -> None:
        """Register task handlers with the queue."""
        if self.task_queue is None:
            return

        # TODO Phase 4: Register review_staged_entry handler
        # TODO Phase 5: Register process_feedback handler
        # TODO Phase 6: Register scheduled task handlers

        # For now, register placeholder handlers
        from knowledge_curator.core.models import TaskType

        async def placeholder_handler(task: QueuedTask) -> None:
            logger.warning(
                f"Placeholder handler for {task.task_type.value} - not implemented"
            )
            raise NotImplementedError(
                f"Handler for {task.task_type.value} not implemented"
            )

        for task_type in TaskType:
            self.task_queue.register_handler(task_type, placeholder_handler)

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

    async def stop(self) -> None:
        """Stop the daemon gracefully."""
        logger.info("Stopping Knowledge Curator daemon...")

        # Signal queue to stop
        if self.task_queue:
            self.task_queue.stop()

        # Signal uvicorn to stop
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()

        # Wait for tasks to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Close database connection
        if self.repository:
            await self.repository.close()
            logger.info("Database connection closed")

        self._shutdown_event.set()
        logger.info("Knowledge Curator daemon stopped")

    async def wait_for_shutdown(self) -> None:
        """Wait until shutdown is complete."""
        await self._shutdown_event.wait()

    async def health_check(self) -> dict[str, object]:
        """Perform health check.

        Returns:
            Health status dictionary.
        """
        status: dict[str, object] = {
            "status": "healthy",
            "database": "unknown",
            "queue": {},
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

        return status


def setup_signal_handlers(
    daemon: CuratorDaemon, loop: asyncio.AbstractEventLoop
) -> None:
    """Set up signal handlers for graceful shutdown.

    Args:
        daemon: Daemon instance.
        loop: Event loop.
    """

    def signal_handler(sig: signal.Signals) -> None:
        logger.info(f"Received signal {sig.name}, initiating shutdown...")
        loop.create_task(daemon.stop())

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

    # Configure logging
    logger.remove()
    logger.add(
        "logs/curator.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
    )
    logger.add(
        lambda msg: print(msg, end=""),
        level=settings.log_level,
        format="{time:HH:mm:ss} | {level: <8} | {message}",
        colorize=True,
    )

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
