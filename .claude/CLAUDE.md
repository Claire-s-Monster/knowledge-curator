# Knowledge Curator Daemon

**Project**: knowledge-curator
**Type**: Standalone LLM-powered daemon (NOT an MCP server)
**Port**: 4005 (webhook receiver only)

## Quick Context

This daemon curates entries for the UCKN knowledge base by:
1. Receiving webhooks from knowledge-bridge (4003)
2. Reviewing staged learnings with LLM evaluation
3. Processing feedback to update quality scores
4. Running scheduled batch operations (dedup, obsolescence, gaps)

## Key Files

| File | Purpose |
|------|---------|
| `PRD.md` | Full requirements document |
| `curator-config.yaml` | Runtime configuration |
| `src/knowledge_curator/daemon.py` | Main daemon loop |
| `src/knowledge_curator/tasks/` | Task implementations |
| `src/knowledge_curator/llm/` | LLM client and prompts |

## Architecture

```
knowledge-bridge:4003 ──webhooks──> Curator:4005 ──API──> UCKN:4004
                                        │
                                        └──> Anthropic API
```

## Development Commands

```bash
# Run daemon (foreground)
pixi run curator

# Run with debug logging
pixi run curator-dev

# Run tests
pixi run test

# Lint and format
pixi run lint
pixi run format
```

## Key Decisions

1. **Standalone daemon** - Not an MCP server, runs as background process
2. **Sonnet default** - Use claude-sonnet-4 for curation, Haiku for simple tasks
3. **Persistent queue** - SQLite-backed task queue survives restarts
4. **Rate limited** - Token bucket rate limiter for API calls

## Task Types

| Task | Trigger | Model |
|------|---------|-------|
| `review_staged_entry` | Webhook | Sonnet |
| `process_feedback` | Webhook | Haiku (mostly DB ops) |
| `deduplicate` | Hourly | Sonnet |
| `detect_obsolescence` | Daily | Sonnet |
| `identify_gaps` | Weekly | Sonnet |

## Testing Strategy

- Mock LLM client for unit tests
- In-memory SQLite for queue tests
- Mock HTTP clients for integration tests

## Related Projects

- **knowledge-bridge**: Sends webhooks, receives decisions
- **UCKN**: Knowledge storage, similarity search
- **session-intelligence**: Source of learnings

## Session Priming

```
/0_agent_priming knowledge-curator daemon

Context:
- PRD: ~/ClaudeCode/Project/knowledge-curator/development/PRD.md
- Design: ~/ClaudeCode/design-docs/knowledge-bridge-proposal.md
- Type: Standalone daemon (not MCP server)
- Port: 4005 (webhook receiver)

Start with Phase 1: Foundation
```
