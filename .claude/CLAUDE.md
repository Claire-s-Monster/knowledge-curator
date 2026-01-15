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

## Current State (2026-01-07)

**Phase 7 COMPLETE - SDK Integration Issue RESOLVED** ✅

### Recent Commits
| Commit | Description |
|--------|-------------|
| `532b617e` | Phase 7 observability and polish (17 files) |
| `0db74eb2` | fix(logging): use sink function for JSON format |
| `ffd2f9e7` | fix(llm): capture SDK stderr for debugging |

### ✅ RESOLVED: SDK Credentials Issue (2026-01-07)

**Problem**: `review_staged_entry` tasks failing with exit code 1, 400 billing error in DLQ (19 entries)

**Root Cause**:
1. **Wrong credentials**: systemd service loaded `~/ClaudeCode/.env` with different API key (low balance account)
2. **Daemon init bug**: Skipped LLM client creation when no explicit API key set, but SDK uses CLI credentials automatically

**Diagnosis**:
- Manual SDK test: ✅ Success ($0.24 charged to MAX subscription)
- Daemon execution: ❌ Failed with "credit balance too low" error
- Same user/UID for both (memento:1000)
- SDK stderr showed billing error from different account

**Solution Applied**:
```bash
# 1. Disabled .env file loading in systemd service
File: ~/.config/systemd/user/knowledge-curator.service
Line 52: # EnvironmentFile=-%h/ClaudeCode/.env  # DISABLED: Use Claude CLI credentials

# 2. Always create LLM client (SDK handles credential fallback)
File: src/knowledge_curator/daemon.py
Line 88-92: self.llm_client = CuratorLLMClient(self.settings)  # SDK uses CLI creds
```

**Outcome**:
- ✅ Tasks complete successfully (DLQ: 19→18)
- ✅ Using MAX subscription credentials
- ✅ First retry: $0.0177 charge, 3+139 tokens
- ✅ LLM client now always initialized

**Key Learnings** (logged to session-intelligence):
- `learn_500f3cc82d08`: systemd EnvironmentFile vars override ~/.claude/.credentials.json
- `learn_969f837edffc`: Always instantiate SDK client wrappers - SDK handles credential fallback internally

### Files Modified
| File | Changes | Committed |
|------|---------|-----------|
| `src/knowledge_curator/llm/client.py` | Added stderr capture, debug logging | No |
| `src/knowledge_curator/daemon.py` | Always create LLM client | No |
| `src/knowledge_curator/tasks/review.py` | Added debug logging | No |
| `src/knowledge_curator/tasks/dedup.py` | Added debug logging | No |
| `~/.config/systemd/user/knowledge-curator.service` | Disabled EnvironmentFile | No |

### Service Status
```bash
systemctl --user status knowledge-curator  # ✅ Running with CLI credentials
curl http://127.0.0.1:4005/health          # ✅ Healthy
curl http://127.0.0.1:4005/dlq/stats       # 18 in DLQ (decreasing)
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
