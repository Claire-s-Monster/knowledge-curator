# Knowledge Curator

LLM-powered knowledge curation daemon for the UCKN knowledge base.

## Overview

Knowledge Curator is a standalone daemon that:
- Receives webhooks from knowledge-bridge (port 4003)
- Reviews staged learnings with LLM evaluation
- Processes feedback to update quality scores
- Runs scheduled batch operations (dedup, obsolescence, gaps)

**Port**: 4005

## Quick Start

```bash
# Run daemon (foreground)
pixi run curator

# Run with debug logging
pixi run curator-dev

# Run with metrics (production)
pixi run -e production curator
```

## API Endpoints

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhooks/curator` | POST | Receive webhook events |
| `/health` | GET | Health check |
| `/status` | GET | Detailed status |
| `/metrics` | GET | Prometheus metrics |

### DLQ Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/dlq` | GET | List DLQ entries |
| `/dlq/stats` | GET | DLQ statistics |
| `/dlq/{id}` | GET | Get DLQ entry |
| `/dlq/{id}/retry` | POST | Retry DLQ entry |
| `/dlq/{id}` | DELETE | Delete DLQ entry |

## Webhook Events

| Event | Trigger |
|-------|---------|
| `learning.staged` | New entry to review |
| `outcome.reported` | Feedback received |
| `curation.requested` | Manual review request |

## Scheduled Tasks

| Task | Schedule | Description |
|------|----------|-------------|
| `deduplicate` | Hourly | Find and merge duplicates |
| `detect_obsolescence` | Daily 3AM | Archive stale entries |
| `identify_gaps` | Weekly Sun 5AM | Find knowledge gaps |

## Configuration

### Environment Variables

```bash
ANTHROPIC_API_KEY=sk-ant-...
CURATOR_WEBHOOK_PORT=4005
CURATOR_DB_PATH=./curator.db
CURATOR_LOG_LEVEL=INFO
CURATOR_KNOWLEDGE_BRIDGE_URL=http://localhost:4003
CURATOR_KNOWLEDGE_STORE_URL=http://localhost:4004
```

### YAML Config

```yaml
# curator-config.yaml
models:
  # Aliases resolve to the latest model in each tier. They cannot go stale the
  # way a dated ID can -- but the name is the only thing that is stable: the
  # model an alias points at changes over time, and so does its price.
  default: "sonnet"
  simple_tasks: "haiku"
  complex_tasks: "opus"

schedules:
  deduplication: "0 * * * *"
  obsolescence: "0 3 * * *"
  gap_analysis: "0 5 * * 0"

shutdown:
  timeout_seconds: 30
  drain_queue: false
```

## Graceful Shutdown

- First signal (SIGTERM/SIGINT): Graceful shutdown
- Second signal: Force immediate shutdown
- Waits for current task to complete (configurable timeout)

## Metrics

Prometheus metrics at `/metrics`:

| Metric | Type | Description |
|--------|------|-------------|
| `curator_tasks_processed_total` | Counter | Tasks processed |
| `curator_task_duration_seconds` | Histogram | Task duration |
| `curator_llm_tokens_total` | Counter | LLM tokens used |
| `curator_queue_depth` | Gauge | Queue depth by status |
| `curator_decisions_total` | Counter | Decisions by type |

## Development

```bash
# Run tests
pixi run -e quality test

# Run with coverage
pixi run -e quality test-cov

# Quality checks
pixi run -e quality quality
```

## Architecture

```
knowledge-bridge:4003 ──webhooks──> Curator:4005 ──API──> knowledge-store:4004
                                        │
                                        └──> Anthropic API (Claude)
```

## Related Projects

- **knowledge-bridge**: Orchestration layer
- **knowledge-store**: ChromaDB-based knowledge storage
- **session-intelligence**: Per-session state and learnings
