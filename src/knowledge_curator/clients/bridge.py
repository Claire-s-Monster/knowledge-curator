"""Knowledge-bridge client for knowledge-curator.

Async HTTP client for interacting with knowledge-bridge server at port 4003.
Provides methods for getting staged entries, notifying decisions, and webhook management.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from knowledge_curator.config import Settings

logger = logging.getLogger(__name__)


class StagedEntry(BaseModel):
    """A staged entry awaiting review."""

    id: str = Field(description="Entry unique identifier")
    source: Literal["session-intelligence", "direct"]
    source_id: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    created_at: str | None = None
    curator_notes: str | None = None


class CurationDecision(BaseModel):
    """Decision made by curator for a staged entry."""

    entry_id: str
    decision: Literal["promote", "reject", "merge", "flag_human"]
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    merged_content: dict[str, Any] | None = None
    similar_entries: list[str] = Field(default_factory=list)


class KnowledgeBridgeClient:
    """Async HTTP client for knowledge-bridge.

    Provides methods to:
    - Get staged entries for review
    - Notify bridge of curation decisions
    - Register/unregister webhook subscriptions
    """

    def __init__(
        self,
        base_url: str = "http://localhost:4003",
        timeout: float = 30.0,
    ) -> None:
        """Initialize knowledge-bridge client.

        Args:
            base_url: Base URL for knowledge-bridge server.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> KnowledgeBridgeClient:
        """Create client from application settings.

        Args:
            settings: Application settings.

        Returns:
            Configured knowledge-bridge client.
        """
        return cls(base_url=settings.knowledge_bridge_url)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        """Check if knowledge-bridge server is healthy.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            client = await self._get_client()
            response = await client.get("/health")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Knowledge-bridge health check failed: {e}")
            return False

    async def get_staged_entries(
        self,
        status: str = "pending",
        limit: int = 50,
    ) -> list[StagedEntry]:
        """Get entries from the staging queue.

        Args:
            status: Filter by status (pending, reviewing, all).
            limit: Maximum entries to return.

        Returns:
            List of staged entries.
        """
        client = await self._get_client()

        try:
            # Use the lean MCP interface
            response = await client.post(
                "/mcp/execute",
                json={
                    "tool_name": "get_staging_queue",
                    "parameters": {
                        "status": status,
                        "limit": limit,
                    },
                },
            )
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                logger.error(f"Get staging queue failed: {data.get('error')}")
                return []

            entries = []
            for entry_data in data.get("result", []):
                entries.append(StagedEntry(**entry_data))
            return entries

        except httpx.HTTPStatusError as e:
            logger.error(f"Get staged entries failed: {e.response.status_code}")
            return []
        except Exception as e:
            logger.error(f"Get staged entries error: {e}")
            return []

    async def get_staged_entry(self, entry_id: str) -> StagedEntry | None:
        """Get a specific staged entry by ID.

        Args:
            entry_id: Entry unique identifier.

        Returns:
            Staged entry if found, None otherwise.
        """
        # For now, fetch all pending entries and filter
        # TODO: Add dedicated endpoint in knowledge-bridge
        entries = await self.get_staged_entries(status="all", limit=500)
        for entry in entries:
            if entry.id == entry_id:
                return entry
        return None

    async def notify_decision(self, decision: CurationDecision) -> bool:
        """Notify knowledge-bridge of curation decision.

        Args:
            decision: The curation decision made.

        Returns:
            True if notification succeeded.
        """
        client = await self._get_client()

        try:
            response = await client.post(
                "/curator/decision",
                json=decision.model_dump(),
            )
            response.raise_for_status()
            return True

        except httpx.HTTPStatusError as e:
            logger.error(f"Notify decision failed: {e.response.status_code}")
            return False
        except Exception as e:
            logger.error(f"Notify decision error: {e}")
            return False

    async def register_webhook(
        self,
        subscriber: str,
        events: list[str],
        endpoint: str,
    ) -> str | None:
        """Register a webhook subscription.

        Args:
            subscriber: Subscriber name (e.g., "curator").
            events: Event types to subscribe to.
            endpoint: HTTP endpoint for delivery.

        Returns:
            Webhook ID if successful, None otherwise.
        """
        client = await self._get_client()

        try:
            response = await client.post(
                "/mcp/execute",
                json={
                    "tool_name": "register_webhook",
                    "parameters": {
                        "subscriber": subscriber,
                        "events": events,
                        "endpoint": endpoint,
                    },
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            if data.get("status") != "success":
                logger.error(f"Register webhook failed: {data.get('error')}")
                return None

            result: dict[str, Any] = data.get("result", {})
            webhook_id: str | None = result.get("id")
            return webhook_id

        except httpx.HTTPStatusError as e:
            logger.error(f"Register webhook failed: {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"Register webhook error: {e}")
            return None

    async def unregister_webhook(self, webhook_id: str) -> bool:
        """Unregister a webhook subscription.

        Args:
            webhook_id: Webhook ID to unregister.

        Returns:
            True if successful.
        """
        client = await self._get_client()

        try:
            response = await client.post(
                "/mcp/execute",
                json={
                    "tool_name": "unregister_webhook",
                    "parameters": {"webhook_id": webhook_id},
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            success: bool = data.get("status") == "success"
            return success

        except httpx.HTTPStatusError as e:
            logger.error(f"Unregister webhook failed: {e.response.status_code}")
            return False
        except Exception as e:
            logger.error(f"Unregister webhook error: {e}")
            return False

    async def get_search_logs(
        self,
        hours: int = 168,  # 1 week
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Get search logs for gap analysis.

        Args:
            hours: Look back this many hours.
            limit: Maximum logs to return.

        Returns:
            List of search log entries.
        """
        client = await self._get_client()

        try:
            response = await client.get(
                "/analytics/search_logs",
                params={"hours": hours, "limit": limit},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logs: list[dict[str, Any]] = data.get("logs", [])
            return logs

        except httpx.HTTPStatusError as e:
            logger.error(f"Get search logs failed: {e.response.status_code}")
            return []
        except Exception as e:
            logger.error(f"Get search logs error: {e}")
            return []
