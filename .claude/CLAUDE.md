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

## Current State (2026-01-06)

**Phase 7 COMMITTED - SDK Integration Issue Under Investigation**

### Commits Made This Session
| Commit | Description |
|--------|-------------|
| `532b617e` | Phase 7 observability and polish (17 files) |
| `0db74eb2` | fix(logging): use sink function for JSON format |
| `ffd2f9e7` | fix(llm): capture SDK stderr for debugging |

### Active Issue: review_staged_entry Tasks Failing

**Symptom**: `review_staged_entry` tasks fail with exit code 1, go to DLQ (19 entries)

**Working**: Scheduled `deduplicate` tasks complete successfully (hourly)

**Root Cause Analysis**:
1. SDK works in isolation (direct Python, pixi, simulated systemd env)
2. Dedup tasks work in daemon (no LLM call if no duplicates found)
3. Review tasks fail - they DO make LLM calls
4. Initial error was "credit balance too low" (400) - billing/auth issue
5. Later errors show exit code 1 with stderr showing:
   - 403 errors (non-critical, marketplace refresh)
   - Git blocked by hooks (non-critical)

**Key Finding**: The dedup tasks complete without LLM calls when no duplicates exist.
The review_staged_entry tasks always call LLM and fail.

**Hypothesis**: Something in the daemon context causes LLM calls to fail, but only
for certain task types. May be related to:
- Prompt content/size differences
- System prompt differences
- Concurrent task processing interference

### DLQ Status
- 19 `review_staged_entry` tasks in DLQ
- All failed with "Command failed with exit code 1"
- Retry endpoint: `POST /dlq/{id}/retry`

### Files Changed (uncommitted)
| File | Changes |
|------|---------|
| `src/knowledge_curator/llm/client.py` | Added stderr capture, debug-to-stderr |

### Next Steps
1. Compare review vs dedup task prompts/system prompts
2. Check if dedup actually calls LLM (add logging)
3. Test review task with simpler prompt
4. Consider if working directory affects CLI behavior

### Service Status
```bash
systemctl --user status knowledge-curator  # Running
curl http://127.0.0.1:4005/health          # Healthy
curl http://127.0.0.1:4005/dlq/stats       # 19 in DLQ
```

## Session Priming

```
/0_agent_priming knowledge-curator daemon

Context:
- PRD: ~/ClaudeCode/Project/knowledge-curator/development/PRD.md
- Design: ~/ClaudeCode/design-docs/knowledge-bridge-proposal.md
- Type: Standalone daemon (not MCP server)
- Port: 4005 (webhook receiver)

Resume: SDK integration issue - review tasks fail, dedup works
Issue: 19 review_staged_entry in DLQ, exit code 1
```
