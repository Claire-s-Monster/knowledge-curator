# knowledge-curator Session Notes
**Date**: 2026-01-04
**Status**: Phases 1-3 Complete, Ready for Phase 4

## Quick Resume Command
```
/0_agent_priming knowledge-curator
```
Then say: "Continue from Phase 4: Review Task"

---

## Project Overview

- **Type**: Standalone LLM-powered daemon (NOT an MCP server)
- **Port**: 4005 (webhook receiver)
- **Purpose**: Curate entries for UCKN knowledge base
- **PRD**: `PRD.md` (842 lines, 7 phases)

## Completed Phases

### Phase 1: Foundation ✅
- Package structure created
- Configuration loading (pydantic-settings, env + YAML)
- SQLite database schema (task_queue, dead_letter_queue, decision_log, cost_log)
- Persistent task queue with priority and retry
- Basic daemon loop with graceful shutdown

### Phase 2: Webhook Receiver ✅
- FastAPI server on port 4005
- Event handlers for learning.staged, outcome.reported, curation.requested
- Health and status endpoints
- Webhook validation and error handling

### Phase 3: LLM Integration ✅
- Anthropic client wrapper with rate limiting
- Token bucket rate limiter (per-model limits)
- Daily budget tracking and enforcement
- Prompt templates for review, dedup, obsolescence, gaps
- Cost tracking per request

## Remaining Phases

| Phase | Priority | Description |
|-------|----------|-------------|
| **4. Review Task** | P0 | Implement `review_staged_entry` handler |
| 5. Feedback Task | P1 | Implement `process_feedback` handler |
| 6. Scheduled Tasks | P1 | APScheduler + dedup/obsolescence/gaps |
| 7. Observability | P2 | Prometheus metrics, structured logging |

## Test Status

```bash
# Run tests
PYTHONPATH=src pixi run -e quality pytest tests/ -v

# Current: 55 tests passing
# - test_config.py: 5 tests
# - test_repository.py: 10 tests
# - test_queue.py: 7 tests
# - test_webhooks.py: 10 tests
# - test_rate_limiter.py: 15 tests
# - test_prompts.py: 10 tests (actually 9)
```

## Key Files

```
src/knowledge_curator/
├── __init__.py              # v0.1.0
├── __main__.py              # CLI entry point
├── config.py                # Settings with nested configs
├── daemon.py                # Main orchestrator
├── core/
│   ├── models.py            # 12 Pydantic models
│   └── queue.py             # TaskQueue with retry logic
├── database/
│   └── repository.py        # SQLite repository
├── webhooks/
│   ├── models.py            # Webhook event models
│   ├── handlers.py          # Event → Task mapping
│   └── server.py            # FastAPI app
└── llm/
    ├── client.py            # CuratorLLMClient
    ├── rate_limiter.py      # TokenBucket + RateLimiter
    └── prompts.py           # Task-specific prompts
```

## Key Decisions

1. **Standalone daemon** - Not MCP server, independent process
2. **Sonnet default** - claude-sonnet-4 for curation, Haiku for simple tasks
3. **SQLite persistence** - Task queue survives restarts
4. **Token bucket** - Per-model rate limiting with daily budget
5. **Circular import fix** - Don't import TaskQueue in core/__init__.py

## Learnings

1. Use `PYTHONPATH=src` when running pytest
2. Use `datetime.now(UTC)` not `datetime.utcnow()` (Python 3.12+)
3. Import TaskQueue from `core.queue` directly to avoid circular imports

## Phase 4 Implementation Plan

Phase 4 implements the `review_staged_entry` task handler:

1. Create `src/knowledge_curator/tasks/review.py`
2. Implement UCKN client for similarity search
3. Implement review logic with LLM evaluation
4. Register handler in daemon
5. Add tests for review task

The review flow:
```
Webhook → Queue Task → Fetch Entry → Search Similar → LLM Evaluate → Decision
```

Decision types: PROMOTE, REJECT, MERGE, FLAG_HUMAN

---

## Commands

```bash
# Development
pixi run curator          # Run daemon
pixi run curator-dev      # Run with DEBUG logging

# Quality
pixi run lint             # Ruff check
pixi run lint-fix         # Ruff auto-fix
pixi run -e quality test  # Run tests (need PYTHONPATH=src)

# Full test command
PYTHONPATH=src pixi run -e quality pytest tests/ -v
```
