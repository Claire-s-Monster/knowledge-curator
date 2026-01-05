# Product Requirements Document: Knowledge Curator Daemon

**Project**: knowledge-curator
**Version**: 0.1.0
**Status**: Draft - Ready for Review
**Created**: 2026-01-03
**Design Reference**: `~/ClaudeCode/design-docs/knowledge-bridge-proposal.md`

---

## Executive Summary

The **Knowledge Curator** is an LLM-powered standalone daemon that ensures knowledge quality in the knowledge-store ecosystem. It receives webhooks from knowledge-bridge, reviews staged entries, processes feedback, deduplicates similar entries, detects obsolete solutions, and identifies knowledge gaps.

Unlike MCP servers, this is a **background process** that runs continuously, responding to events and scheduled tasks.

## Related Projects

| Project | Port | Purpose | Status |
|---------|------|---------|--------|
| session-intelligence | 4002 | Per-session state, decisions, learnings | Existing |
| knowledge-bridge | 4003 | Orchestration layer | In development |
| knowledge-store | 4004 | Global knowledge base (ChromaDB MCP) | Existing |
| **knowledge-curator** | **4005** | **LLM-powered curation (this project)** | **New** |

## Architecture Decisions (ADRs)

### ADR-001: Standalone Daemon (Not MCP Server)

**Decision**: Curator runs as a standalone daemon process, not as an MCP server.

**Rationale**:
- Can restart/update curator without affecting MCP servers
- Can run on different hardware if needed (GPU for local models)
- Independent resource management (LLM API rate limits)
- Flexibility in model selection per task

**Consequences**:
- Requires webhook subscription management
- Needs its own process supervision (systemd, supervisor, etc.)
- More processes to monitor

**Alternatives Rejected**:
- Background thread in knowledge-store (coupling concerns)
- Claude Code hook triggered (not persistent)
- Scheduled cron job only (no event-driven capability)

### ADR-002: Sonnet as Default Curator Model

**Decision**: Use Claude Sonnet as the default curator model, configurable per task.

**Rationale**:
- Good balance of quality and cost for curation tasks
- Can switch to Haiku for simple operations (similarity checks)
- Can escalate to Opus for complex synthesis (gap analysis)

**Model Cost Reference** (as of 2026):
| Model | Input | Output | Use Case |
|-------|-------|--------|----------|
| Haiku | $0.25/MTok | $1.25/MTok | Simple classification |
| Sonnet | $3/MTok | $15/MTok | Default curation |
| Opus | $15/MTok | $75/MTok | Complex synthesis |

### ADR-003: Event-Driven + Scheduled Hybrid

**Decision**: Combine webhook-driven responses with scheduled batch operations.

**Rationale**:
- Webhooks provide real-time response to new entries
- Schedules handle batch operations (dedup, obsolescence)
- Reduces LLM API calls by batching similar operations

---

## System Architecture

```
                    +------------------+
                    | knowledge-bridge |
                    |     :4003        |
                    +--------+---------+
                             |
                    webhooks | (HTTP POST)
                             v
+----------------+  +------------------+  +----------------+
|    Scheduler   |->|  Knowledge       |->|   Anthropic    |
|   (APScheduler)|  |  Curator Daemon  |  |   API          |
+----------------+  +--------+---------+  +----------------+
                             |
                    HTTP     | requests (MCP JSON-RPC)
                             v
              +-----------------------------+
              |    knowledge-store :4004    |
              |  (ChromaDB MCP Server)      |
              +-----------------------------+
```

## Functional Requirements

### FR-1: Webhook Handler

The daemon must expose an HTTP endpoint to receive webhooks from knowledge-bridge.

#### FR-1.1: Webhook Endpoint

```python
POST /webhooks/curator
Content-Type: application/json

{
    "event_type": "learning.staged",
    "payload": { ... },
    "timestamp": "2026-01-03T10:00:00Z",
    "webhook_id": "wh_abc123"
}
```

**Events to Handle**:
| Event | Action |
|-------|--------|
| `learning.staged` | Queue review_staged_entry task |
| `outcome.reported` | Queue process_feedback task |
| `curation.requested` | Queue manual_review task (high priority) |

#### FR-1.2: Webhook Response

```python
# Success
HTTP 200 OK
{"status": "accepted", "task_id": "task_xyz789"}

# Validation Error
HTTP 400 Bad Request
{"status": "rejected", "error": "Unknown event type"}

# Internal Error
HTTP 500 Internal Server Error
{"status": "error", "error": "Task queue unavailable"}
```

### FR-2: Curation Tasks

#### FR-2.1: review_staged_entry

**Trigger**: `learning.staged` webhook

**Input**:
```python
class StagedEntryReview:
    entry_id: str
    content: dict  # Learning content from knowledge-bridge
    source: str    # "session-intelligence" or "direct"
```

**Process**:
1. Fetch entry details from knowledge-bridge staging queue
2. Search knowledge-store for similar entries (cosine similarity)
3. LLM evaluation with decision tree:

```
                    ┌─────────────────┐
                    │ Fetch Entry     │
                    └────────┬────────┘
                             │
                    ┌────────v────────┐
                    │ Search Similar  │
                    │ in knowledge-store│
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        similarity     similarity     similarity
          > 0.95       0.85-0.95        < 0.85
              │              │              │
              v              v              v
        ┌─────────┐   ┌──────────┐   ┌──────────┐
        │DUPLICATE│   │ PARTIAL  │   │  NOVEL   │
        │ Reject  │   │ OVERLAP  │   │ Evaluate │
        └─────────┘   │ LLM Merge│   │ Quality  │
                      └──────────┘   └──────────┘
                                           │
                              ┌────────────┼────────────┐
                              │            │            │
                         high quality  low quality   unclear
                              │            │            │
                              v            v            v
                        ┌─────────┐  ┌─────────┐  ┌─────────┐
                        │ PROMOTE │  │ REJECT  │  │  FLAG   │
                        │to store │  │ w/reason│  │ human   │
                        └─────────┘  └─────────┘  └─────────┘
```

**Output**:
```python
class ReviewDecision:
    entry_id: str
    decision: Literal["promote", "reject", "merge", "flag_human"]
    confidence: float
    reason: str
    similar_entries: list[str]  # knowledge-store IDs if any
    merged_content: dict | None  # If merge decision
```

**LLM Prompt Structure**:
```python
REVIEW_PROMPT = """
You are a knowledge curator evaluating a learning entry for promotion to a global knowledge base.

## Entry to Evaluate
{entry_content}

## Similar Existing Entries
{similar_entries}

## Evaluation Criteria
1. **Novelty**: Does this add new information not in existing entries?
2. **Quality**: Is the solution correct, complete, and well-explained?
3. **Generalizability**: Will this help future sessions with similar problems?
4. **Specificity**: Is it specific enough to be actionable?

## Decision Options
- PROMOTE: Novel, high-quality, generalizable
- REJECT: Duplicate, low quality, or too specific
- MERGE: Partial overlap - combine with existing entry
- FLAG_HUMAN: Unclear, needs human review

Respond with JSON:
{
    "decision": "promote|reject|merge|flag_human",
    "confidence": 0.0-1.0,
    "reason": "Brief explanation",
    "merge_with": "entry_id if merge",
    "merged_content": "Combined content if merge"
}
"""
```

#### FR-2.2: process_feedback

**Trigger**: `outcome.reported` webhook

**Input**:
```python
class FeedbackReport:
    session_id: str
    knowledge_id: str  # knowledge-store entry ID
    outcome: Literal["success", "failure", "partial"]
    notes: str
```

**Process**:
1. Fetch current knowledge-store entry statistics
2. Update success/failure counts
3. Recalculate quality score
4. Evaluate for status change:

```python
def calculate_quality_score(entry: KnowledgeEntry) -> float:
    """Weighted quality score calculation."""
    if entry.times_applied == 0:
        return 0.5  # Neutral for unused entries

    success_rate = entry.success_count / entry.times_applied
    recency_factor = decay_by_age(entry.last_applied)

    return (0.7 * success_rate) + (0.3 * recency_factor)

def evaluate_status_change(entry: KnowledgeEntry) -> StatusChange | None:
    """Determine if entry status should change."""
    if entry.times_applied >= 5:
        if entry.success_rate >= 0.8:
            return StatusChange("canonical")  # Proven pattern
        elif entry.success_rate <= 0.3:
            return StatusChange("archive")    # Failed pattern
    return None
```

**Output**:
```python
class FeedbackResult:
    knowledge_id: str
    new_quality_score: float
    previous_score: float
    status_change: str | None  # "canonical", "archive", None
    action_taken: str
```

#### FR-2.3: deduplicate (Scheduled)

**Trigger**: Hourly schedule

**Process**:
1. Fetch all knowledge-store entries (or entries modified in last 24h)
2. Compute pairwise similarity using embeddings
3. Cluster entries with similarity > 0.92
4. For each cluster, LLM decides best entry
5. Mark inferior entries as `superseded_by`

**Batching Strategy**:
```python
async def deduplicate_batch():
    """Process entries in batches to control costs."""
    entries = await knowledge_store_client.get_recent_entries(hours=24)

    # Pre-filter with embeddings (cheap)
    clusters = cluster_by_embedding_similarity(entries, threshold=0.92)

    # LLM only for ambiguous clusters (expensive)
    for cluster in clusters:
        if len(cluster) > 1:
            best = await llm_select_best_entry(cluster)
            await mark_others_superseded(cluster, best)
```

**Output**:
```python
class DeduplicationResult:
    clusters_found: int
    entries_superseded: int
    entries_kept: int
    details: list[ClusterResolution]
```

#### FR-2.4: detect_obsolescence (Scheduled)

**Trigger**: Daily schedule (3 AM)

**Process**:
1. Find entries not applied in 30+ days
2. Find entries with declining success rate (3+ consecutive failures)
3. LLM evaluates if solution is still valid:
   - Check if technology has changed
   - Check if better alternatives exist
   - Decide: keep, update, or archive

**Output**:
```python
class ObsolescenceResult:
    entries_reviewed: int
    entries_archived: int
    entries_updated: int
    entries_kept: int
    details: list[ObsolescenceDecision]
```

#### FR-2.5: identify_gaps (Scheduled)

**Trigger**: Weekly schedule (Sunday 5 AM)

**Process**:
1. Analyze `search_log` from knowledge-bridge
2. Find queries with 0 results or low-quality results
3. Cluster similar failed queries
4. Generate "knowledge gaps" report
5. Optionally: LLM generates stub entries for common gaps

**Output**:
```python
class GapAnalysisResult:
    period: str  # "2026-W01"
    total_searches: int
    failed_searches: int
    gap_clusters: list[GapCluster]
    recommendations: list[str]

class GapCluster:
    representative_query: str
    similar_queries: list[str]
    frequency: int
    suggested_pattern_type: str
```

### FR-3: Task Queue

#### FR-3.1: Priority Queue

```python
class TaskPriority(Enum):
    CRITICAL = 0   # Manual review requests
    HIGH = 1       # New staged entries
    NORMAL = 2     # Feedback processing
    LOW = 3        # Scheduled batch tasks
```

#### FR-3.2: Task Persistence

Tasks must survive daemon restarts:
- SQLite database for task queue
- On startup, resume incomplete tasks
- Dead letter queue for failed tasks (3 retries)

### FR-4: Rate Limiting

#### FR-4.1: Anthropic API Rate Limits

```python
class RateLimiter:
    """Token bucket rate limiter for API calls."""

    limits = {
        "haiku": {"rpm": 50, "tpm": 50000},
        "sonnet": {"rpm": 50, "tpm": 40000},
        "opus": {"rpm": 20, "tpm": 20000},
    }
```

#### FR-4.2: Backpressure

When rate limited:
1. Queue tasks instead of dropping
2. Process in FIFO order when capacity available
3. Alert if queue exceeds threshold (100 pending)

---

## Non-Functional Requirements

### NFR-1: Performance

| Metric | Target |
|--------|--------|
| Webhook response time | < 100ms (accept only) |
| review_staged_entry | < 30s including LLM |
| process_feedback | < 5s (mostly DB updates) |
| deduplicate batch (100 entries) | < 5 min |

### NFR-2: Reliability

| Requirement | Implementation |
|-------------|----------------|
| Task persistence | SQLite with WAL |
| Retry policy | 3 attempts, exponential backoff |
| Dead letter queue | After 3 failures, move to DLQ |
| Graceful shutdown | Finish current task, persist queue |

### NFR-3: Observability

| Metric | Type |
|--------|------|
| `curator_tasks_processed` | Counter |
| `curator_task_duration_seconds` | Histogram |
| `curator_llm_tokens_used` | Counter (by model) |
| `curator_queue_depth` | Gauge |
| `curator_decisions` | Counter (by decision type) |

### NFR-4: Cost Management

| Control | Implementation |
|---------|----------------|
| Model tiering | Haiku for simple, Sonnet for default |
| Batch processing | Group similar operations |
| Daily budget | Configurable spend limit |
| Cost tracking | Log tokens used per task |

---

## Database Schema

```sql
-- SQLite database: curator.db

-- Task queue for persistence
CREATE TABLE task_queue (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,      -- 'review', 'feedback', 'dedup', etc.
    priority INTEGER DEFAULT 2,
    payload JSON NOT NULL,
    status TEXT DEFAULT 'pending', -- 'pending', 'processing', 'completed', 'failed'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    retry_count INTEGER DEFAULT 0,
    error_message TEXT
);

-- Dead letter queue for failed tasks
CREATE TABLE dead_letter_queue (
    id TEXT PRIMARY KEY,
    original_task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    payload JSON NOT NULL,
    error_message TEXT,
    failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (original_task_id) REFERENCES task_queue(id)
);

-- Decision log for auditing
CREATE TABLE decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    entry_id TEXT,                -- knowledge-store or staging entry
    decision TEXT NOT NULL,       -- 'promote', 'reject', 'merge', etc.
    confidence REAL,
    reason TEXT,
    model_used TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES task_queue(id)
);

-- Cost tracking
CREATE TABLE cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    model TEXT NOT NULL,
    tokens_input INTEGER NOT NULL,
    tokens_output INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    task_count INTEGER NOT NULL,
    UNIQUE(date, model)
);

-- Indexes
CREATE INDEX idx_task_status ON task_queue(status);
CREATE INDEX idx_task_priority ON task_queue(priority, created_at);
CREATE INDEX idx_decision_entry ON decision_log(entry_id);
CREATE INDEX idx_cost_date ON cost_log(date);
```

---

## Configuration

### Environment Variables

```bash
# API Configuration
ANTHROPIC_API_KEY=sk-ant-...
CURATOR_DEFAULT_MODEL=claude-sonnet-4-20250514
CURATOR_SIMPLE_MODEL=claude-haiku          # For simple tasks
CURATOR_COMPLEX_MODEL=claude-sonnet-4-20250514  # For synthesis

# Service URLs
KNOWLEDGE_BRIDGE_URL=http://localhost:4003
KNOWLEDGE_STORE_URL=http://localhost:4004

# Webhook Server
CURATOR_WEBHOOK_PORT=4005
CURATOR_WEBHOOK_HOST=127.0.0.1

# Database
CURATOR_DB_PATH=./curator.db

# Rate Limiting
CURATOR_DAILY_BUDGET_USD=10.00
CURATOR_MAX_CONCURRENT_LLM=3

# Scheduling
CURATOR_DEDUP_CRON="0 * * * *"           # Hourly
CURATOR_OBSOLESCENCE_CRON="0 3 * * *"    # Daily 3 AM
CURATOR_GAP_ANALYSIS_CRON="0 5 * * 0"    # Sunday 5 AM

# Thresholds
CURATOR_SIMILARITY_DUPLICATE=0.95
CURATOR_SIMILARITY_RELATED=0.85
CURATOR_SUCCESS_RATE_CANONICAL=0.8
CURATOR_SUCCESS_RATE_ARCHIVE=0.3
CURATOR_DAYS_UNUSED_ARCHIVE=30
```

### Configuration File

```yaml
# curator-config.yaml

models:
  default: "claude-sonnet-4-20250514"
  simple_tasks: "claude-haiku"
  complex_tasks: "claude-sonnet-4-20250514"

schedules:
  deduplication: "0 * * * *"      # Hourly
  quality_recalc: "0 3 * * *"     # Daily at 3am
  obsolescence: "0 3 * * *"       # Daily at 3am
  gap_analysis: "0 5 * * 0"       # Weekly Sunday 5am

thresholds:
  similarity_duplicate: 0.95
  similarity_related: 0.85
  success_rate_archive: 0.3
  success_rate_canonical: 0.8
  min_applications_canonical: 5
  days_unused_archive: 30

rate_limits:
  daily_budget_usd: 10.00
  max_concurrent_llm: 3

retry:
  max_attempts: 3
  backoff_base_seconds: 5
  backoff_multiplier: 2

webhooks:
  bridge_url: "http://localhost:4003"
  subscribe_events:
    - "learning.staged"
    - "outcome.reported"
    - "curation.requested"
```

---

## Project Structure

```
~/ClaudeCode/Project/knowledge-curator/development/
├── .claude/
│   ├── CLAUDE.md                  # Project-specific instructions
│   └── commands/                  # Linked from framework
├── src/
│   └── knowledge_curator/
│       ├── __init__.py
│       ├── __main__.py            # Entry point
│       ├── daemon.py              # Main daemon loop
│       ├── config.py              # Configuration loading
│       ├── core/
│       │   ├── __init__.py
│       │   ├── models.py          # Pydantic models
│       │   ├── tasks.py           # Task definitions
│       │   └── queue.py           # Task queue management
│       ├── tasks/
│       │   ├── __init__.py
│       │   ├── review.py          # review_staged_entry
│       │   ├── feedback.py        # process_feedback
│       │   ├── dedup.py           # deduplicate
│       │   ├── obsolescence.py    # detect_obsolescence
│       │   └── gaps.py            # identify_gaps
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── client.py          # Anthropic API client
│       │   ├── prompts.py         # Task-specific prompts
│       │   └── rate_limiter.py    # Token bucket rate limiter
│       ├── clients/
│       │   ├── __init__.py
│       │   ├── bridge.py          # Client for knowledge-bridge
│       │   └── knowledge_store.py # Client for knowledge-store
│       ├── webhooks/
│       │   ├── __init__.py
│       │   ├── server.py          # Webhook receiver (FastAPI)
│       │   └── handlers.py        # Event handlers
│       ├── scheduler/
│       │   ├── __init__.py
│       │   └── scheduler.py       # APScheduler integration
│       └── database/
│           ├── __init__.py
│           └── repository.py      # SQLite operations
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_review.py
│   ├── test_feedback.py
│   ├── test_dedup.py
│   ├── test_gaps.py
│   └── test_queue.py
├── pyproject.toml
├── pixi.lock
├── curator-config.yaml
├── PRD.md                         # This file
└── curator.db                     # SQLite (runtime, gitignored)
```

---

## Implementation Phases

### Phase 1: Foundation (Priority: P0)

- [ ] Project scaffolding with pixi
- [ ] Configuration loading (env + yaml)
- [ ] SQLite database setup with schema
- [ ] Task queue implementation (persistent)
- [ ] Basic daemon loop (process queue)

**Deliverable**: Daemon starts, processes tasks from queue, persists state.

### Phase 2: Webhook Receiver (Priority: P0)

- [ ] FastAPI webhook server
- [ ] Event validation and parsing
- [ ] Queue task on webhook receipt
- [ ] Register with knowledge-bridge on startup
- [ ] Health endpoint

**Deliverable**: Receives webhooks from knowledge-bridge, queues tasks.

### Phase 3: LLM Integration (Priority: P0)

- [ ] Anthropic client wrapper
- [ ] Rate limiter (token bucket)
- [ ] Cost tracking
- [ ] Model selection per task type
- [ ] Prompt templates

**Deliverable**: Can make LLM calls with rate limiting and cost tracking.

### Phase 4: Review Task (Priority: P0)

- [ ] Implement review_staged_entry
- [x] knowledge-store similarity search integration
- [ ] Decision tree logic
- [ ] Notify knowledge-bridge of decision

**Deliverable**: Reviews staged entries and makes promote/reject/merge decisions.

### Phase 5: Feedback Task (Priority: P1)

- [ ] Implement process_feedback
- [ ] Quality score calculation
- [ ] Status change detection
- [ ] knowledge-store update integration

**Deliverable**: Processes feedback to update entry quality scores.

### Phase 6: Scheduled Tasks (Priority: P1)

- [ ] APScheduler integration
- [ ] Implement deduplicate
- [ ] Implement detect_obsolescence
- [ ] Implement identify_gaps

**Deliverable**: Scheduled batch operations run automatically.

### Phase 7: Observability & Polish (Priority: P2)

- [ ] Prometheus metrics
- [ ] Structured logging
- [ ] Graceful shutdown handling
- [ ] Dead letter queue processing
- [ ] Documentation

**Deliverable**: Production-ready daemon with full observability.

---

## Testing Strategy

### Unit Tests

- Task functions with mocked LLM client
- Queue operations with in-memory SQLite
- Rate limiter behavior
- Configuration parsing

### Integration Tests

- Webhook receipt to task execution
- Full review flow with mock knowledge-store
- Scheduler triggering tasks

### Load Tests

- Queue backpressure under high webhook volume
- Rate limiter behavior at limits
- Memory usage over extended operation

---

## Deployment

### Development

```bash
# Start daemon in foreground
pixi run curator

# Start with debug logging
pixi run curator-dev
```

### Production

```bash
# Systemd service
sudo systemctl start knowledge-curator
sudo systemctl enable knowledge-curator
```

**Systemd Unit File** (`/etc/systemd/system/knowledge-curator.service`):
```ini
[Unit]
Description=Knowledge Curator Daemon
After=network.target

[Service]
Type=simple
User=memento
WorkingDirectory=/home/memento/ClaudeCode/Project/knowledge-curator/development
ExecStart=/home/memento/.pixi/bin/pixi run curator
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Success Criteria

1. **Webhook handling**: < 100ms response, 100% delivery acknowledgment
2. **Review accuracy**: > 90% agreement with human review (sampled)
3. **Cost efficiency**: < $10/day for typical workload
4. **Queue depth**: < 50 pending tasks under normal load
5. **Uptime**: > 99% (excluding planned maintenance)

---

## Open Questions

1. **Human review interface**: How will flagged entries be reviewed?
   - Option A: Dashboard in observability UI
   - Option B: GitHub issues
   - Option C: Slack notifications

2. **Multi-tenant support**: Should curator support multiple knowledge-store instances?
   - Current assumption: Single knowledge-store instance

3. **Feedback loop bootstrapping**: How to handle cold start with no feedback data?
   - Option: Manual seeding with known-good patterns

4. **Gap analysis output**: Where should gap reports be stored/displayed?
   - Option: Weekly report in knowledge-bridge database

---

## References

- Design Document: `~/ClaudeCode/design-docs/knowledge-bridge-proposal.md`
- Knowledge-Bridge PRD: `~/ClaudeCode/Servers/knowledge-bridge/development/PRD.md`
- Knowledge-Store: `~/ClaudeCode/Servers/knowledge-store/development/`
- Session-Intelligence: `~/ClaudeCode/Servers/session-intelligence/`

---

## Changelog

| Date | Change |
|------|--------|
| 2026-01-03 | Initial PRD created |
| 2026-01-05 | Migrated from UCKN to knowledge-store (MCP JSON-RPC) |
