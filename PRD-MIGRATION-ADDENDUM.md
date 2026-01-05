# PRD Addendum: UCKN to Knowledge-Store Migration

**Date**: 2026-01-05
**Status**: Approved
**Parent PRD**: PRD.md (Knowledge Curator Daemon)

---

## Executive Summary

The Knowledge Curator daemon will no longer use UCKN (Universal Claude Knowledge Network) as its knowledge backend. Instead, it will integrate with **knowledge-store**, a ChromaDB-based MCP server providing semantic search over development patterns.

This is a **breaking change** that affects:
- Client code for knowledge backend communication
- Configuration (URL and naming)
- API patterns (REST → MCP JSON-RPC over HTTP)

---

## Rationale

1. **Consolidation**: knowledge-store provides all required functionality (semantic search, CRUD, quality metrics)
2. **MCP Pattern**: Aligns with ecosystem-wide MCP server adoption
3. **Maintained**: knowledge-store is actively maintained in `~/ClaudeCode/Servers/knowledge-store/`
4. **Simpler**: Fewer services to deploy and manage

---

## API Comparison

### Old: UCKN REST API

| Operation | Endpoint | Method |
|-----------|----------|--------|
| Health check | `/health` | GET |
| Search similar | `/patterns/search` | POST |
| Get pattern | `/patterns/{id}` | GET |
| Update pattern | `/patterns/{id}` | PATCH |
| Contribute pattern | `/patterns/contribute` | POST |
| Validate pattern | `/patterns/{id}/validate` | PUT |
| List recent | `/patterns` | GET |

### New: Knowledge-Store MCP (JSON-RPC over HTTP)

All operations use the lean 3-tool MCP pattern via `POST /mcp`:

| Operation | MCP Tool | execute_tool Parameters |
|-----------|----------|------------------------|
| Health check | N/A | Use `GET /health` endpoint |
| Search | `search` | `{query, limit, filters}` |
| Find similar | `find_similar` | `{entry_id, threshold, limit}` |
| Get entry | `get_entry` | `{entry_id}` |
| Add entry | `add_entry` | `{problem_pattern, solution, ...}` |
| Update entry | `update_entry` | `{entry_id, updates}` |
| Delete entry | `delete_entry` | `{entry_id}` |
| List entries | `list_entries` | `{filters, limit, offset}` |
| Get stats | `get_stats` | `{}` |

### MCP JSON-RPC Format

```json
// Request
{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "execute_tool",
        "arguments": {
            "tool_name": "search",
            "parameters": {
                "query": "pytest fixture not found",
                "limit": 10
            }
        }
    }
}

// Response
{
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "content": [{"type": "text", "text": "{'results': [...], 'count': 3}"}]
    }
}
```

---

## Data Model Changes

### UCKN Pattern Model

```python
class Pattern:
    id: str
    document: str           # Combined content
    metadata: dict          # Arbitrary metadata
```

### Knowledge-Store Entry Model

```python
class KnowledgeEntry:
    # Identity
    id: str

    # Content (embedded for search)
    problem_pattern: str    # What problem this solves
    solution: str           # The solution/pattern
    code_example: str | None

    # Classification
    tags: list[str]
    pattern_type: Literal["bugfix", "best_practice", "optimization", "setup", "architecture"]

    # Quality metrics
    times_applied: int
    success_count: int
    failure_count: int
    quality_score: float    # 0.0 to 1.0

    # Lifecycle
    status: Literal["active", "canonical", "archived", "superseded"]
    superseded_by: str | None

    # Provenance
    source_session: str | None
    source_type: Literal["session", "direct", "seeded"]
    created_at: datetime
    updated_at: datetime
    last_applied_at: datetime | None
```

### Mapping: UCKN → Knowledge-Store

| UCKN Field | Knowledge-Store Field | Notes |
|------------|----------------------|-------|
| `id` | `id` | Same (UUID) |
| `document` | `problem_pattern + solution + code_example` | Split content fields |
| `metadata.title` | `problem_pattern` | First sentence |
| `metadata.description` | `solution` | Full solution |
| `metadata.tags` | `tags` | Same |
| `metadata.type` | `pattern_type` | Enum values aligned |
| `metadata.quality_score` | `quality_score` | Same |
| `metadata.status` | `status` | Same values |

---

## Configuration Changes

### Current (PRD.md)

```python
class Settings:
    uckn_url: str = "http://localhost:4004"
```

### New

```python
class Settings:
    knowledge_store_url: str = "http://localhost:4004"  # Renamed
```

### Environment Variables

| Old | New |
|-----|-----|
| `UCKN_URL` | `CURATOR_KNOWLEDGE_STORE_URL` |

---

## Client Implementation Changes

### Current: clients/uckn.py

```python
class UCKNClient:
    async def search_similar(self, query: str, limit: int = 10, ...) -> list[PatternSearchResult]:
        response = await client.post("/patterns/search", json=request_body)
        # Returns: list of patterns with similarity_score
```

### New: clients/knowledge_store.py

```python
class KnowledgeStoreClient:
    async def search(self, query: str, limit: int = 10, ...) -> list[SearchResult]:
        result = await self._execute_tool("search", {"query": query, "limit": limit})
        # Returns: list of entries with similarity_score

    async def _execute_tool(self, tool_name: str, parameters: dict) -> dict:
        """Call MCP execute_tool via JSON-RPC over HTTP."""
        response = await client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": "execute_tool",
                "arguments": {"tool_name": tool_name, "parameters": parameters}
            }
        })
        return parse_mcp_response(response)
```

---

## Code Changes Required

### 1. New Client Module

**File**: `src/knowledge_curator/clients/knowledge_store.py`

Create new client implementing MCP JSON-RPC protocol with:
- `search()` - Semantic search for entries
- `find_similar()` - Find duplicates by entry ID
- `get_entry()` - Get single entry by ID
- `add_entry()` - Create new entry
- `update_entry()` - Update entry fields
- `list_entries()` - List with filters
- `health_check()` - Server health
- Compatibility aliases for existing task code

### 2. Configuration Update

**File**: `src/knowledge_curator/config.py`

```python
# Change:
uckn_url: str = Field(default="http://localhost:4004", description="UCKN service URL")

# To:
knowledge_store_url: str = Field(default="http://localhost:4004", description="Knowledge Store service URL")
```

### 3. Import Updates

**Files affected**:
- `src/knowledge_curator/daemon.py`
- `src/knowledge_curator/tasks/review.py`
- `src/knowledge_curator/tasks/dedup.py`
- `src/knowledge_curator/tasks/obsolescence.py`
- `src/knowledge_curator/tasks/gaps.py`

```python
# Change:
from knowledge_curator.clients.uckn import UCKNClient

# To:
from knowledge_curator.clients.knowledge_store import KnowledgeStoreClient
```

### 4. Task Updates

Update task implementations to use new client methods and data models:

#### review.py
- `uckn_client.search_similar()` → `knowledge_store_client.search()`
- Update result parsing for `SearchResult` model

#### dedup.py
- Use `find_similar()` for duplicate detection
- `update_entry()` with `superseded_by` field

#### obsolescence.py
- Use `list_entries(filters={"status": "active"})`
- `update_entry()` for status changes

#### gaps.py
- Integrate with knowledge-store statistics via `get_stats()`

### 5. Client __init__.py Update

**File**: `src/knowledge_curator/clients/__init__.py`

```python
# Change:
from knowledge_curator.clients.uckn import UCKNClient, PatternSearchResult

# To:
from knowledge_curator.clients.knowledge_store import (
    KnowledgeStoreClient,
    KnowledgeEntry,
    SearchResult,
)
```

### 6. Remove UCKN Client

**Action**: Delete `src/knowledge_curator/clients/uckn.py` after migration complete

---

## Test Updates

### Unit Tests

**File**: `tests/test_clients.py`

- Update to test `KnowledgeStoreClient`
- Mock MCP JSON-RPC responses instead of REST
- Test response parsing from stringified dict format

### Conftest

**File**: `tests/conftest.py`

```python
@pytest.fixture
def mock_knowledge_store_client():
    """Mock knowledge-store MCP client."""
    client = AsyncMock(spec=KnowledgeStoreClient)
    client.health_check.return_value = True
    client.search.return_value = []
    return client
```

---

## Environment Setup

### Test Environment Fix

Current issue: `test` task runs in `default` environment which lacks `aiosqlite`.

**Action**: Update test invocation to use `quality` environment:

```bash
# Instead of:
pixi run test

# Use:
pixi run -e quality test
```

**Or** add `aiosqlite` to the default environment in `pyproject.toml`:

```toml
[tool.pixi.dependencies]
# Add:
aiosqlite = ">=0.20"
```

---

## Migration Steps

### Phase 1: Preparation (No Breaking Changes)

1. Create `clients/knowledge_store.py` with new client
2. Add compatibility aliases to maintain existing interface
3. Update tests to work with both clients
4. Fix test environment (aiosqlite)

### Phase 2: Configuration Migration

1. Add `knowledge_store_url` to Settings
2. Deprecate `uckn_url` (keep as alias temporarily)
3. Update environment variable documentation

### Phase 3: Code Migration

1. Update all imports to use new client
2. Update task implementations for new data model
3. Run full test suite
4. Fix mypy type errors

### Phase 4: Cleanup

1. Remove `uckn.py` client
2. Remove deprecated config aliases
3. Update PRD.md references from UCKN to knowledge-store
4. Update architecture diagram

---

## Type Errors to Fix

Current mypy shows 11 errors in 4 files. After migration, address:

1. `rate_limiter.py:292` - Missing type parameters for `dict`
2. `client.py:157` - Anthropic messages type incompatibility
3. `client.py:172` - Union type attribute access on content blocks
4. `client.py:258` - Missing type parameters for `dict`
5. `server.py:48` - Missing return type annotation
6. `daemon.py:100` - `QueuedTask` name not defined
7. `daemon.py:179` - Missing type parameters for `dict`
8. `daemon.py:222` - Indexed assignment on Collection
9. `daemon.py:246` - Cannot infer lambda type

---

## Port Assignment

| Service | Port | Notes |
|---------|------|-------|
| session-intelligence | 4002 | HTTP transport |
| knowledge-bridge | 4003 | Webhook sender |
| **knowledge-store** | **4004** | MCP server (replaces UCKN) |
| knowledge-curator | 4005 | Webhook receiver |

The default port 4004 remains unchanged since knowledge-store replaces UCKN.

---

## Success Criteria

1. All existing tests pass with new client
2. Mypy strict mode passes (0 errors)
3. Lint passes (Ruff)
4. Curator can successfully:
   - Search knowledge-store for similar entries
   - Add new entries via webhook handling
   - Update entry quality scores
   - Detect duplicates
5. No references to UCKN remain in codebase

---

## Timeline

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | Create new client | Pending |
| Phase 2 | Configuration migration | Pending |
| Phase 3 | Code migration | Pending |
| Phase 4 | Cleanup | Pending |

---

## References

- Knowledge-Store: `~/ClaudeCode/Servers/knowledge-store/development/`
- Knowledge-Store Models: `src/knowledge_store/models.py`
- Knowledge-Store Server: `src/knowledge_store/server.py`
- Knowledge-Store HTTP Transport: `src/knowledge_store/transport/http.py`
