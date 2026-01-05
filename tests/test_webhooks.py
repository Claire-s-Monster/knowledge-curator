"""Tests for webhook server and handlers."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from knowledge_curator.config import Settings
from knowledge_curator.core.models import TaskPriority, TaskType
from knowledge_curator.core.queue import TaskQueue
from knowledge_curator.database.repository import Repository
from knowledge_curator.webhooks.handlers import WebhookHandlers
from knowledge_curator.webhooks.models import WebhookEvent, WebhookEventType
from knowledge_curator.webhooks.server import WebhookServer


@pytest.fixture
async def task_queue(tmp_path: Path) -> TaskQueue:
    """Create a task queue with test database."""
    db_path = tmp_path / "test_webhooks.db"
    repo = Repository(db_path)
    await repo.connect()

    settings = Settings(db_path=db_path, log_level="DEBUG")
    queue = TaskQueue(repo, settings)
    yield queue
    await repo.close()


@pytest.fixture
def webhook_handlers(task_queue: TaskQueue) -> WebhookHandlers:
    """Create webhook handlers with task queue."""
    return WebhookHandlers(task_queue)


class TestWebhookHandlers:
    """Tests for WebhookHandlers class."""

    @pytest.mark.asyncio
    async def test_handle_learning_staged(
        self, webhook_handlers: WebhookHandlers, task_queue: TaskQueue
    ) -> None:
        """Test handling learning.staged events."""
        event = WebhookEvent(
            event_type=WebhookEventType.LEARNING_STAGED,
            payload={
                "entry_id": "entry-123",
                "content": {"pattern": "test pattern", "solution": "test solution"},
                "source": "session-intelligence",
                "session_id": "session-456",
                "metadata": {"key": "value"},
            },
            timestamp=datetime.now(UTC),
            webhook_id="wh-001",
        )

        task_id = await webhook_handlers.handle_event(event)

        assert task_id is not None
        task = await task_queue.repo.get_task(task_id)
        assert task is not None
        assert task.task_type == TaskType.REVIEW_STAGED
        assert task.priority == TaskPriority.HIGH
        assert task.payload["entry_id"] == "entry-123"
        assert task.payload["webhook_id"] == "wh-001"

    @pytest.mark.asyncio
    async def test_handle_outcome_reported(
        self, webhook_handlers: WebhookHandlers, task_queue: TaskQueue
    ) -> None:
        """Test handling outcome.reported events."""
        event = WebhookEvent(
            event_type=WebhookEventType.OUTCOME_REPORTED,
            payload={
                "session_id": "session-789",
                "knowledge_id": "uckn-entry-001",
                "outcome": "success",
                "notes": "Pattern worked well",
                "context": {"file": "main.py"},
            },
            timestamp=datetime.now(UTC),
            webhook_id="wh-002",
        )

        task_id = await webhook_handlers.handle_event(event)

        assert task_id is not None
        task = await task_queue.repo.get_task(task_id)
        assert task is not None
        assert task.task_type == TaskType.PROCESS_FEEDBACK
        assert task.priority == TaskPriority.NORMAL
        assert task.payload["knowledge_id"] == "uckn-entry-001"
        assert task.payload["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_handle_curation_requested(
        self, webhook_handlers: WebhookHandlers, task_queue: TaskQueue
    ) -> None:
        """Test handling curation.requested events."""
        event = WebhookEvent(
            event_type=WebhookEventType.CURATION_REQUESTED,
            payload={
                "entry_id": "entry-456",
                "reason": "Unclear if this is a duplicate",
                "requester": "user@example.com",
                "priority": "high",
            },
            timestamp=datetime.now(UTC),
            webhook_id="wh-003",
        )

        task_id = await webhook_handlers.handle_event(event)

        assert task_id is not None
        task = await task_queue.repo.get_task(task_id)
        assert task is not None
        assert task.task_type == TaskType.MANUAL_REVIEW
        assert task.priority == TaskPriority.HIGH
        assert task.payload["reason"] == "Unclear if this is a duplicate"

    @pytest.mark.asyncio
    async def test_handle_curation_critical_priority(
        self, webhook_handlers: WebhookHandlers, task_queue: TaskQueue
    ) -> None:
        """Test that critical priority is respected."""
        event = WebhookEvent(
            event_type=WebhookEventType.CURATION_REQUESTED,
            payload={
                "entry_id": "entry-789",
                "reason": "Urgent review needed",
                "requester": "admin",
                "priority": "critical",
            },
            timestamp=datetime.now(UTC),
            webhook_id="wh-004",
        )

        task_id = await webhook_handlers.handle_event(event)

        task = await task_queue.repo.get_task(task_id)
        assert task is not None
        assert task.priority == TaskPriority.CRITICAL

    @pytest.mark.asyncio
    async def test_handle_unknown_event_type(
        self, webhook_handlers: WebhookHandlers
    ) -> None:
        """Test that unknown event types raise ValueError."""
        # Create event with mocked unknown type
        event = MagicMock(spec=WebhookEvent)
        event.event_type = "unknown.event"

        with pytest.raises(ValueError, match="Unknown event type"):
            await webhook_handlers.handle_event(event)


class TestWebhookServer:
    """Tests for WebhookServer using TestClient."""

    @pytest.fixture
    def mock_daemon(self, tmp_path: Path) -> MagicMock:
        """Create a mock daemon for testing."""
        daemon = MagicMock()
        daemon.settings = Settings(
            db_path=tmp_path / "test.db",
            webhook_host="127.0.0.1",
            webhook_port=4005,
            log_level="DEBUG",
        )
        daemon.task_queue = MagicMock()
        daemon.task_queue.enqueue = AsyncMock(return_value="task-123")
        daemon.health_check = AsyncMock(
            return_value={
                "status": "healthy",
                "database": "connected",
                "queue": {"pending": 5},
            }
        )
        return daemon

    def test_health_endpoint(self, mock_daemon: MagicMock) -> None:
        """Test health check endpoint."""
        server = WebhookServer(mock_daemon)

        with TestClient(server.app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "connected"
        assert "uptime_seconds" in data

    def test_status_endpoint(self, mock_daemon: MagicMock) -> None:
        """Test detailed status endpoint."""
        server = WebhookServer(mock_daemon)

        with TestClient(server.app) as client:
            response = client.get("/status")

        assert response.status_code == 200
        data = response.json()
        assert "version" in data
        assert "config" in data
        assert data["config"]["webhook_port"] == 4005

    def test_webhook_endpoint_valid_event(self, mock_daemon: MagicMock) -> None:
        """Test webhook endpoint with valid event."""
        server = WebhookServer(mock_daemon)

        with TestClient(server.app) as client:
            # Patch handlers after lifespan starts
            server.handlers = MagicMock()
            server.handlers.handle_event = AsyncMock(return_value="task-456")

            response = client.post(
                "/webhooks/curator",
                json={
                    "event_type": "learning.staged",
                    "payload": {
                        "entry_id": "entry-123",
                        "content": {"test": "data"},
                    },
                    "timestamp": "2026-01-04T12:00:00Z",
                    "webhook_id": "wh-test",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert data["task_id"] == "task-456"

    def test_webhook_endpoint_invalid_payload(self, mock_daemon: MagicMock) -> None:
        """Test webhook endpoint with invalid payload."""
        server = WebhookServer(mock_daemon)

        with TestClient(server.app) as client:
            response = client.post(
                "/webhooks/curator",
                json={
                    "invalid": "payload",
                },
            )

        assert response.status_code == 200  # Returns 200 with rejected status
        data = response.json()
        assert data["status"] == "rejected"
        assert "error" in data

    def test_webhook_endpoint_no_handlers(self, mock_daemon: MagicMock) -> None:
        """Test webhook endpoint when handlers not initialized."""
        # Set task_queue to None so handlers won't be created
        mock_daemon.task_queue = None
        server = WebhookServer(mock_daemon)

        with TestClient(server.app) as client:
            response = client.post(
                "/webhooks/curator",
                json={
                    "event_type": "learning.staged",
                    "payload": {"entry_id": "test", "content": {}},
                    "timestamp": "2026-01-04T12:00:00Z",
                    "webhook_id": "wh-test",
                },
            )

        assert response.status_code == 503
        assert "not ready" in response.json()["detail"]
