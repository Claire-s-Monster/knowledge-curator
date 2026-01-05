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
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import ValidationError

from knowledge_curator import __version__
from knowledge_curator.webhooks.handlers import WebhookHandlers
from knowledge_curator.webhooks.models import (
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
