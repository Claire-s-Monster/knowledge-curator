"""Knowledge Store client for knowledge-curator.

Async HTTP client for interacting with the Knowledge Store MCP server.
Uses JSON-RPC over HTTP to communicate with the lean 3-tool pattern.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from knowledge_curator.config import Settings

logger = logging.getLogger(__name__)


class KnowledgeEntry(BaseModel):
    """A knowledge entry from the store."""

    id: str = Field(description="Entry unique identifier")
    problem_pattern: str = Field(description="What problem this solves")
    solution: str = Field(description="The solution/pattern")
    code_example: str | None = Field(default=None, description="Optional code snippet")
    tags: list[str] = Field(default_factory=list)
    pattern_type: str = Field(default="bugfix")
    quality_score: float = Field(default=0.5)
    times_applied: int = Field(default=0)
    success_count: int = Field(default=0)
    failure_count: int = Field(default=0)
    status: str = Field(default="active")
    superseded_by: str | None = None
    source_session: str | None = None
    source_type: str = Field(default="session")


class SearchResult(BaseModel):
    """Result from semantic search."""

    entry: KnowledgeEntry
    similarity_score: float = Field(ge=0.0, le=1.0)


class KnowledgeStoreClient:
    """Async HTTP client for Knowledge Store MCP server.

    Uses the lean 3-tool MCP pattern via HTTP JSON-RPC:
    - discover_tools: List available operations
    - get_tool_spec: Get operation schema
    - execute_tool: Execute an operation
    """

    def __init__(
        self,
        base_url: str = "http://localhost:4004",
        timeout: float = 30.0,
    ) -> None:
        """Initialize Knowledge Store client.

        Args:
            base_url: Base URL for Knowledge Store server.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> KnowledgeStoreClient:
        """Create client from application settings.

        Args:
            settings: Application settings.

        Returns:
            Configured Knowledge Store client.
        """
        return cls(base_url=settings.knowledge_store_url)

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

    def _next_request_id(self) -> int:
        """Get next JSON-RPC request ID."""
        self._request_id += 1
        return self._request_id

    async def _call_mcp(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a JSON-RPC call to the MCP endpoint.

        Args:
            method: JSON-RPC method name.
            params: Method parameters.

        Returns:
            The result from the response.

        Raises:
            httpx.HTTPStatusError: On HTTP errors.
            ValueError: On JSON-RPC errors.
        """
        client = await self._get_client()

        request_body = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": method,
            "params": params or {},
        }

        response = await client.post("/mcp", json=request_body)
        response.raise_for_status()

        data = response.json()
        if "error" in data:
            error = data["error"]
            raise ValueError(f"MCP error {error.get('code')}: {error.get('message')}")

        result: dict[str, Any] = data.get("result", {})
        return result

    async def _execute_tool(
        self,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a tool via MCP.

        Args:
            tool_name: Name of the tool to execute.
            parameters: Tool parameters.

        Returns:
            Tool execution result.
        """
        result = await self._call_mcp(
            "tools/call",
            {
                "name": "execute_tool",
                "arguments": {
                    "tool_name": tool_name,
                    "parameters": parameters,
                },
            },
        )

        # Parse the text content from MCP response
        content = result.get("content", [])
        if content:
            text = content[0].get("text", "{}")
            # The result is a string representation of a dict
            try:
                # Try Python literal first (handles single quotes, etc.)
                import ast

                return ast.literal_eval(text)  # type: ignore[no-any-return]
            except (ValueError, SyntaxError):
                # Fall back to JSON
                return json.loads(text)  # type: ignore[no-any-return]
        return {}

    async def health_check(self) -> bool:
        """Check if Knowledge Store server is healthy.

        Returns:
            True if healthy, False otherwise.
        """
        try:
            client = await self._get_client()
            response = await client.get("/health")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Knowledge Store health check failed: {e}")
            return False

    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search for knowledge entries using semantic search.

        Args:
            query: Search query text.
            limit: Maximum number of results.
            filters: Optional metadata filters.

        Returns:
            List of matching entries with similarity scores.
        """
        try:
            result = await self._execute_tool(
                "search",
                {
                    "query": query,
                    "limit": limit,
                    "filters": filters or {},
                },
            )

            results = []
            for item in result.get("results", []):
                entry_data = item.get("entry", {})
                results.append(
                    SearchResult(
                        entry=KnowledgeEntry(**entry_data),
                        similarity_score=item.get("similarity_score", 0.0),
                    )
                )
            return results

        except Exception as e:
            logger.error(f"Knowledge Store search error: {e}")
            raise

    async def find_similar(
        self,
        entry_id: str,
        threshold: float = 0.85,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Find entries similar to a given entry (for deduplication).

        Args:
            entry_id: Entry UUID to find similar entries for.
            threshold: Minimum similarity score.
            limit: Maximum number of results.

        Returns:
            List of similar entries with similarity scores.
        """
        try:
            result = await self._execute_tool(
                "find_similar",
                {
                    "entry_id": entry_id,
                    "threshold": threshold,
                    "limit": limit,
                },
            )

            results = []
            for item in result.get("results", []):
                entry_data = item.get("entry", {})
                results.append(
                    SearchResult(
                        entry=KnowledgeEntry(**entry_data),
                        similarity_score=item.get("similarity_score", 0.0),
                    )
                )
            return results

        except Exception as e:
            logger.error(f"Knowledge Store find_similar error: {e}")
            raise

    async def get_entry(self, entry_id: str) -> KnowledgeEntry | None:
        """Get an entry by ID.

        Args:
            entry_id: Entry unique identifier.

        Returns:
            Entry if found, None otherwise.
        """
        try:
            result = await self._execute_tool(
                "get_entry",
                {"entry_id": entry_id},
            )
            entry_data = result.get("entry")
            if entry_data:
                return KnowledgeEntry(**entry_data)
            return None

        except Exception as e:
            logger.error(f"Knowledge Store get_entry error: {e}")
            raise

    async def add_entry(
        self,
        problem_pattern: str,
        solution: str,
        code_example: str | None = None,
        tags: list[str] | None = None,
        pattern_type: str = "bugfix",
        source_session: str | None = None,
        source_type: str = "session",
    ) -> str | None:
        """Add a new knowledge entry.

        Args:
            problem_pattern: What problem this solves.
            solution: The solution/pattern.
            code_example: Optional code snippet.
            tags: Classification tags.
            pattern_type: Type of pattern.
            source_session: Source session ID.
            source_type: How entry was created.

        Returns:
            Created entry ID if successful, None otherwise.
        """
        try:
            params: dict[str, Any] = {
                "problem_pattern": problem_pattern,
                "solution": solution,
                "pattern_type": pattern_type,
                "source_type": source_type,
            }
            if code_example:
                params["code_example"] = code_example
            if tags:
                params["tags"] = tags
            if source_session:
                params["source_session"] = source_session

            result = await self._execute_tool("add_entry", params)
            return result.get("entry_id")

        except Exception as e:
            logger.error(f"Knowledge Store add_entry error: {e}")
            return None

    async def update_entry(
        self,
        entry_id: str,
        updates: dict[str, Any],
    ) -> bool:
        """Update an entry's metadata or status.

        Args:
            entry_id: Entry unique identifier.
            updates: Fields to update.

        Returns:
            True if update succeeded.
        """
        try:
            result = await self._execute_tool(
                "update_entry",
                {
                    "entry_id": entry_id,
                    "updates": updates,
                },
            )
            success = result.get("success", False)
            return bool(success)

        except Exception as e:
            logger.error(f"Knowledge Store update_entry error: {e}")
            return False

    async def list_entries(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KnowledgeEntry]:
        """List entries with optional filtering.

        Args:
            filters: Metadata filters.
            limit: Maximum entries to return.
            offset: Pagination offset.

        Returns:
            List of entries.
        """
        try:
            result = await self._execute_tool(
                "list_entries",
                {
                    "filters": filters or {},
                    "limit": limit,
                    "offset": offset,
                },
            )

            return [
                KnowledgeEntry(**entry_data) for entry_data in result.get("entries", [])
            ]

        except Exception as e:
            logger.error(f"Knowledge Store list_entries error: {e}")
            return []

    async def get_stats(self) -> dict[str, Any]:
        """Get collection statistics.

        Returns:
            Statistics about the knowledge store.
        """
        try:
            return await self._execute_tool("get_stats", {})
        except Exception as e:
            logger.error(f"Knowledge Store get_stats error: {e}")
            return {}

    # Compatibility aliases for migration from UCKN client
    async def search_similar(
        self,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.7,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Alias for search() with UCKN-compatible signature.

        Args:
            query: Search query text.
            limit: Maximum number of results.
            min_similarity: Minimum similarity threshold (used as filter).
            metadata_filter: Optional metadata filters.

        Returns:
            List of matching entries with similarity scores.
        """
        filters = metadata_filter.copy() if metadata_filter else {}
        if min_similarity > 0:
            filters["min_similarity"] = min_similarity
        return await self.search(query, limit, filters)

    async def get_pattern(self, pattern_id: str) -> dict[str, Any] | None:
        """Alias for get_entry() with UCKN-compatible signature.

        Args:
            pattern_id: Pattern/Entry unique identifier.

        Returns:
            Entry data as dict if found, None otherwise.
        """
        entry = await self.get_entry(pattern_id)
        if entry:
            return entry.model_dump()
        return None

    async def update_pattern(
        self,
        pattern_id: str,
        updates: dict[str, Any],
    ) -> bool:
        """Alias for update_entry() with UCKN-compatible signature.

        Args:
            pattern_id: Pattern/Entry unique identifier.
            updates: Fields to update.

        Returns:
            True if update succeeded.
        """
        return await self.update_entry(pattern_id, updates)

    async def contribute_pattern(
        self,
        document: str,
        metadata: dict[str, Any],
        project_id: str | None = None,
    ) -> str | None:
        """Alias for add_entry() with UCKN-compatible signature.

        Args:
            document: Pattern content/document.
            metadata: Pattern metadata.
            project_id: Optional associated project ID (ignored).

        Returns:
            Created entry ID if successful, None otherwise.
        """
        # Parse document into problem_pattern and solution
        parts = document.split("\n\n", 1)
        problem_pattern = parts[0] if parts else document
        solution = parts[1] if len(parts) > 1 else ""

        return await self.add_entry(
            problem_pattern=metadata.get("title", problem_pattern),
            solution=metadata.get("description", solution),
            code_example=metadata.get("code_example"),
            tags=metadata.get("tags", []),
            pattern_type=metadata.get("type", "bugfix"),
            source_session=metadata.get("source_session"),
            source_type=metadata.get("source_type", "session"),
        )

    async def validate_pattern(
        self,
        pattern_id: str,
        success: bool,
        feedback: str | None = None,
        score: float | None = None,
    ) -> bool:
        """Alias for update_entry() with validation semantics.

        Args:
            pattern_id: Pattern/Entry to validate.
            success: Whether validation passed.
            feedback: Optional feedback text (stored in notes).
            score: Optional validation score (0.0-1.0).

        Returns:
            True if validation was recorded.
        """
        # Fetch current entry to update counts
        entry = await self.get_entry(pattern_id)
        if not entry:
            return False

        updates: dict[str, Any] = {
            "times_applied": entry.times_applied + 1,
        }

        if success:
            updates["success_count"] = entry.success_count + 1
        else:
            updates["failure_count"] = entry.failure_count + 1

        if score is not None:
            updates["quality_score"] = score

        return await self.update_entry(pattern_id, updates)

    async def get_recent_entries(
        self,
        hours: int = 24,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Alias for list_entries() with UCKN-compatible signature.

        Args:
            hours: Look back this many hours (filter by updated_at).
            limit: Maximum entries to return.

        Returns:
            List of recently modified entries as dicts.
        """
        filters = {"modified_within_hours": hours}
        entries = await self.list_entries(filters=filters, limit=limit)
        return [entry.model_dump() for entry in entries]
