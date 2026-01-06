"""FastAPI webhook server for Knowledge Curator.

Provides HTTP endpoints for:
- Webhook reception from knowledge-bridge
- Health checks
- Status information
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger
from pydantic import ValidationError

from knowledge_curator import __version__
from knowledge_curator.core.models import TaskPriority, TaskType
from knowledge_curator.metrics import get_metrics
from knowledge_curator.webhooks.handlers import WebhookHandlers
from knowledge_curator.webhooks.models import (
    DLQListResponse,
    DLQRetryRequest,
    DLQRetryResponse,
    DLQStatsResponse,
    HealthResponse,
    WebhookEvent,
    WebhookResponse,
)

if TYPE_CHECKING:
    from knowledge_curator.daemon import CuratorDaemon


class WebhookServer:
    """FastAPI-based webhook server."""

    def __init__(self, daemon: "CuratorDaemon") -> None:
        """Initialize webhook server.

        Args:
            daemon: Parent daemon instance for accessing components.
        """
        self.daemon = daemon
        self.handlers: WebhookHandlers | None = None
        self.start_time: float = 0
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        """Create and configure FastAPI application."""

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
            """Manage server lifecycle."""
            self.start_time = time.time()
            if self.daemon.task_queue:
                self.handlers = WebhookHandlers(self.daemon.task_queue)
            logger.info("Webhook server started")
            yield
            logger.info("Webhook server stopped")

        app = FastAPI(
            title="Knowledge Curator",
            description="LLM-powered knowledge curation daemon",
            version=__version__,
            lifespan=lifespan,
        )

        # Register routes
        app.add_api_route(
            "/webhooks/curator",
            self._handle_webhook,
            methods=["POST"],
            response_model=WebhookResponse,
            summary="Receive webhook events",
            description="Endpoint for knowledge-bridge to send events",
        )

        app.add_api_route(
            "/health",
            self._health_check,
            methods=["GET"],
            response_model=HealthResponse,
            summary="Health check",
            description="Check daemon health status",
        )

        app.add_api_route(
            "/status",
            self._get_status,
            methods=["GET"],
            summary="Detailed status",
            description="Get detailed daemon status information",
        )

        # DLQ Management Endpoints
        app.add_api_route(
            "/dlq",
            self._list_dlq,
            methods=["GET"],
            response_model=DLQListResponse,
            summary="List DLQ entries",
            description="List entries in the dead letter queue",
        )

        app.add_api_route(
            "/dlq/stats",
            self._dlq_stats,
            methods=["GET"],
            response_model=DLQStatsResponse,
            summary="DLQ statistics",
            description="Get dead letter queue statistics",
        )

        app.add_api_route(
            "/dlq/{dlq_id}",
            self._get_dlq_entry,
            methods=["GET"],
            summary="Get DLQ entry",
            description="Get a specific dead letter queue entry",
        )

        app.add_api_route(
            "/dlq/{dlq_id}/retry",
            self._retry_dlq_entry,
            methods=["POST"],
            response_model=DLQRetryResponse,
            summary="Retry DLQ entry",
            description="Move a DLQ entry back to the task queue for retry",
        )

        app.add_api_route(
            "/dlq/{dlq_id}",
            self._delete_dlq_entry,
            methods=["DELETE"],
            summary="Delete DLQ entry",
            description="Permanently delete a DLQ entry",
        )

        # Metrics endpoint
        app.add_api_route(
            "/metrics",
            self._get_metrics,
            methods=["GET"],
            summary="Prometheus metrics",
            description="Prometheus-format metrics for monitoring",
        )

        # Add exception handler
        app.add_exception_handler(Exception, self._handle_exception)

        return app

    async def _handle_webhook(self, request: Request) -> WebhookResponse:
        """Handle incoming webhook events.

        Args:
            request: FastAPI request object.

        Returns:
            Webhook response with status and task ID.
        """
        if self.handlers is None:
            logger.error("Webhook handlers not initialized")
            raise HTTPException(
                status_code=503,
                detail="Service not ready",
            )

        try:
            # Parse request body
            body = await request.json()
            event = WebhookEvent.model_validate(body)

            logger.info(
                f"Received webhook: type={event.event_type}, "
                f"webhook_id={event.webhook_id}"
            )

            # Handle the event
            task_id = await self.handlers.handle_event(event)

            return WebhookResponse(
                status="accepted",
                task_id=task_id,
            )

        except ValidationError as e:
            logger.warning(f"Invalid webhook payload: {e}")
            return WebhookResponse(
                status="rejected",
                error=f"Invalid payload: {e.error_count()} validation errors",
            )

        except ValueError as e:
            logger.warning(f"Webhook processing error: {e}")
            return WebhookResponse(
                status="rejected",
                error=str(e),
            )

        except Exception as e:
            logger.exception(f"Webhook handling error: {e}")
            return WebhookResponse(
                status="error",
                error="Internal server error",
            )

    async def _health_check(self) -> HealthResponse:
        """Perform health check.

        Returns:
            Health status response.
        """
        health = await self.daemon.health_check()

        queue_data = health.get("queue", {})
        return HealthResponse(
            status=str(health["status"]),
            version=__version__,
            database=str(health["database"]),
            queue=queue_data if isinstance(queue_data, dict) else {},
            uptime_seconds=time.time() - self.start_time,
        )

    async def _get_status(self) -> dict[str, Any]:
        """Get detailed daemon status.

        Returns:
            Detailed status dictionary.
        """
        health = await self.daemon.health_check()
        queue_stats = health.get("queue", {})

        return {
            "version": __version__,
            "status": health["status"],
            "uptime_seconds": time.time() - self.start_time,
            "database": {
                "status": health["database"],
                "path": str(self.daemon.settings.db_path),
            },
            "queue": queue_stats,
            "config": {
                "webhook_host": self.daemon.settings.webhook_host,
                "webhook_port": self.daemon.settings.webhook_port,
                "log_level": self.daemon.settings.log_level,
                "knowledge_bridge_url": self.daemon.settings.knowledge_bridge_url,
                "knowledge_store_url": self.daemon.settings.knowledge_store_url,
            },
        }

    # -------------------------------------------------------------------------
    # DLQ Management Methods
    # -------------------------------------------------------------------------

    async def _list_dlq(
        self,
        limit: int = 100,
        offset: int = 0,
        task_type: str | None = None,
    ) -> DLQListResponse:
        """List entries in the dead letter queue.

        Args:
            limit: Maximum entries to return.
            offset: Number of entries to skip.
            task_type: Filter by task type.

        Returns:
            List of DLQ entries.
        """
        if not self.daemon.repository:
            raise HTTPException(status_code=503, detail="Database not connected")

        # Parse task type filter if provided
        type_filter = None
        if task_type:
            try:
                type_filter = TaskType(task_type)
            except ValueError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid task_type: {task_type}",
                ) from e

        entries = await self.daemon.repository.get_dlq_entries(
            limit=limit,
            offset=offset,
            task_type=type_filter,
        )
        total = await self.daemon.repository.get_dlq_count()

        return DLQListResponse(
            entries=[entry.model_dump(mode="json") for entry in entries],
            total=total,
            limit=limit,
            offset=offset,
        )

    async def _dlq_stats(self) -> DLQStatsResponse:
        """Get DLQ statistics.

        Returns:
            DLQ statistics.
        """
        if not self.daemon.repository:
            raise HTTPException(status_code=503, detail="Database not connected")

        total = await self.daemon.repository.get_dlq_count()
        by_type = await self.daemon.repository.get_dlq_stats()

        return DLQStatsResponse(
            total=total,
            by_task_type=by_type,
        )

    async def _get_dlq_entry(self, dlq_id: str) -> dict[str, Any]:
        """Get a specific DLQ entry.

        Args:
            dlq_id: DLQ entry identifier.

        Returns:
            DLQ entry data.
        """
        if not self.daemon.repository:
            raise HTTPException(status_code=503, detail="Database not connected")

        entry = await self.daemon.repository.get_dlq_entry(dlq_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="DLQ entry not found")

        return entry.model_dump(mode="json")

    async def _retry_dlq_entry(
        self,
        dlq_id: str,
        request: DLQRetryRequest | None = None,
    ) -> DLQRetryResponse:
        """Retry a DLQ entry by moving it back to the task queue.

        Args:
            dlq_id: DLQ entry identifier.
            request: Optional retry configuration.

        Returns:
            Retry result.
        """
        if not self.daemon.repository:
            raise HTTPException(status_code=503, detail="Database not connected")

        # Map priority string to enum
        priority_map = {
            "critical": TaskPriority.CRITICAL,
            "high": TaskPriority.HIGH,
            "normal": TaskPriority.NORMAL,
            "low": TaskPriority.LOW,
        }
        priority_str = request.priority if request else "high"
        priority = priority_map.get(priority_str.lower(), TaskPriority.HIGH)

        new_task_id = await self.daemon.repository.retry_from_dlq(
            dlq_id=dlq_id,
            priority=priority,
        )

        if new_task_id is None:
            return DLQRetryResponse(
                status="error",
                error="DLQ entry not found",
            )

        logger.info(
            f"Retried DLQ entry {dlq_id[:8]}... as new task {new_task_id[:8]}..."
        )

        return DLQRetryResponse(
            status="success",
            new_task_id=new_task_id,
        )

    async def _delete_dlq_entry(self, dlq_id: str) -> dict[str, Any]:
        """Permanently delete a DLQ entry.

        Args:
            dlq_id: DLQ entry identifier.

        Returns:
            Deletion result.
        """
        if not self.daemon.repository:
            raise HTTPException(status_code=503, detail="Database not connected")

        deleted = await self.daemon.repository.delete_from_dlq(dlq_id)

        if not deleted:
            raise HTTPException(status_code=404, detail="DLQ entry not found")

        logger.info(f"Deleted DLQ entry {dlq_id[:8]}...")

        return {"status": "deleted", "dlq_id": dlq_id}

    async def _get_metrics(self) -> Response:
        """Get Prometheus metrics.

        Returns:
            Prometheus text format metrics response.
        """
        metrics = get_metrics()

        # Update queue metrics before returning
        if self.daemon.task_queue:
            await metrics.update_queue_metrics(self.daemon.task_queue)

        return Response(
            content=metrics.generate_metrics(),
            media_type=metrics.get_content_type(),
        )

    async def _handle_exception(self, request: Request, exc: Exception) -> JSONResponse:
        """Global exception handler.

        Args:
            request: FastAPI request.
            exc: Exception that occurred.

        Returns:
            JSON error response.
        """
        logger.exception(f"Unhandled exception: {exc}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Internal server error"},
        )


def create_webhook_app(daemon: "CuratorDaemon") -> FastAPI:
    """Create webhook server FastAPI app.

    Args:
        daemon: Parent daemon instance.

    Returns:
        Configured FastAPI application.
    """
    server = WebhookServer(daemon)
    return server.app
