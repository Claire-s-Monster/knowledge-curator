"""Webhook event handlers for Knowledge Curator.

Maps webhook events to task queue operations.
"""

from loguru import logger

from knowledge_curator.core.models import TaskPriority, TaskType
from knowledge_curator.core.queue import TaskQueue
from knowledge_curator.webhooks.models import (
    CurationRequestPayload,
    OutcomePayload,
    StagedLearningPayload,
    WebhookEvent,
    WebhookEventType,
)


class WebhookHandlers:
    """Handles webhook events by creating appropriate tasks."""

    def __init__(self, task_queue: TaskQueue) -> None:
        """Initialize handlers with task queue.

        Args:
            task_queue: Task queue for creating tasks.
        """
        self.task_queue = task_queue

    async def handle_event(self, event: WebhookEvent) -> str:
        """Route event to appropriate handler.

        Args:
            event: Webhook event to handle.

        Returns:
            Task ID of created task.

        Raises:
            ValueError: If event type is unknown.
        """
        handlers = {
            WebhookEventType.LEARNING_STAGED: self._handle_learning_staged,
            WebhookEventType.OUTCOME_REPORTED: self._handle_outcome_reported,
            WebhookEventType.CURATION_REQUESTED: self._handle_curation_requested,
        }

        handler = handlers.get(event.event_type)
        if handler is None:
            raise ValueError(f"Unknown event type: {event.event_type}")

        return await handler(event)

    async def _handle_learning_staged(self, event: WebhookEvent) -> str:
        """Handle learning.staged events.

        Creates a review_staged_entry task with HIGH priority.

        Args:
            event: Webhook event.

        Returns:
            Task ID.
        """
        payload = StagedLearningPayload.model_validate(event.payload)

        logger.info(
            f"Handling learning.staged: entry_id={payload.entry_id}, "
            f"source={payload.source}"
        )

        task_id = await self.task_queue.enqueue(
            task_type=TaskType.REVIEW_STAGED,
            payload={
                "entry_id": payload.entry_id,
                "content": payload.content,
                "source": payload.source,
                "session_id": payload.session_id,
                "metadata": payload.metadata,
                "webhook_id": event.webhook_id,
            },
            priority=TaskPriority.HIGH,
        )

        return task_id

    async def _handle_outcome_reported(self, event: WebhookEvent) -> str:
        """Handle outcome.reported events.

        Creates a process_feedback task with NORMAL priority.

        Args:
            event: Webhook event.

        Returns:
            Task ID.
        """
        payload = OutcomePayload.model_validate(event.payload)

        logger.info(
            f"Handling outcome.reported: knowledge_id={payload.knowledge_id}, "
            f"outcome={payload.outcome}"
        )

        task_id = await self.task_queue.enqueue(
            task_type=TaskType.PROCESS_FEEDBACK,
            payload={
                "session_id": payload.session_id,
                "knowledge_id": payload.knowledge_id,
                "outcome": payload.outcome,
                "notes": payload.notes,
                "context": payload.context,
                "webhook_id": event.webhook_id,
            },
            priority=TaskPriority.NORMAL,
        )

        return task_id

    async def _handle_curation_requested(self, event: WebhookEvent) -> str:
        """Handle curation.requested events (manual review).

        Creates a manual_review task with priority based on request.

        Args:
            event: Webhook event.

        Returns:
            Task ID.
        """
        payload = CurationRequestPayload.model_validate(event.payload)

        # Map priority string to TaskPriority
        priority_map = {
            "low": TaskPriority.LOW,
            "normal": TaskPriority.NORMAL,
            "high": TaskPriority.HIGH,
            "critical": TaskPriority.CRITICAL,
        }
        priority = priority_map.get(payload.priority, TaskPriority.NORMAL)

        logger.info(
            f"Handling curation.requested: entry_id={payload.entry_id}, "
            f"priority={payload.priority}, requester={payload.requester}"
        )

        task_id = await self.task_queue.enqueue(
            task_type=TaskType.MANUAL_REVIEW,
            payload={
                "entry_id": payload.entry_id,
                "reason": payload.reason,
                "requester": payload.requester,
                "webhook_id": event.webhook_id,
            },
            priority=priority,
        )

        return task_id
