---
title: "Phase 4: MCP Server + Inter-Worker Messaging"
status: SHIPPED
shipped_date: 2026-04-01
related_specs:
  - sqlite-unified-storage.md
---

# Phase 4: MCP Server + Inter-Worker Messaging — Specification

> **Status: SHIPPED (2026-04-01).** All four sub-deliverables (4.1–4.4) are live. The MCP server lives in `src/swarm/mcp/`, the message store in `src/swarm/messages/`, and the route handlers in `src/swarm/server/routes/messages.py`. Since Phase 5 of the SQLite migration, the `messages` table lives in the unified `~/.swarm/swarm.db` — **not** a separate `messages.db` as this spec originally described. Retained as a historical reference.
>
> Scoped via interview on 2026-04-01. Derived from Claude Code source analysis.

## Overview

Phase 4 replaces Swarm's file-based hook communication with a native MCP (Model Context Protocol) server that workers connect to directly. Workers gain bidirectional messaging, task management, and coordination tools via Claude Code's native MCP client. The existing hook system is retained as a fallback.

## Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Primary interface | MCP server (SSE on existing daemon) | Single process, reuses aiohttp, no extra infra |
| Hook future | Keep as fallback | Already working, zero maintenance, covers non-MCP providers |
| Approval model | Stays as PreToolUse hook | Hooks intercept synchronously; MCP is for worker-initiated actions |
| Message delivery | Pull model (worker calls `swarm_check_messages`) | No injection timing problem, worker decides when to check |
| Check frequency | System prompt guidance | "Check messages at task boundaries" — cheap, natural |
| Message persistence | SQLite (originally `~/.swarm/messages.db`, now consolidated into `~/.swarm/swarm.db` `messages` table) | Survives restarts, same pattern as drone log store |
| Volume control | Dedup + rate limit per (sender, recipient) pair, 60s window | Prevents message storms from rapid operations |
| Worker namespace | Flat — all workers are peers by name | Matches existing task board model |
| MCP registration | Global (`~/.claude/settings.json`) | All workers connect to same daemon regardless of repo |
| MCP auth | `SWARM_MANAGED=1` env var | Daemon already trusts managed workers for hook calls |
| Server identity | Single server "swarm", all tools visible | Simple, uniform. Workers self-regulate via prompts |
| Dashboard | Messages in buzz log with "message" category badge | Reuses existing log infra |
| Operator messaging | Yes — dashboard send input, delivered via same channel | Replaces PTY injection for giving workers instructions |
| Delivery split | 4 sub-deliverables (4.1 → 4.4) | Ship independently, reduce blast radius |

## MCP Tools (8 total)

### Core messaging (4.2)
| Tool | Description | Input | Output |
|------|-------------|-------|--------|
| `swarm_check_messages` | Read pending messages | `{}` (worker self-identifies) | `{messages: [{from, type, content, ts}]}` |
| `swarm_send_message` | Send message to a worker | `{to, type, content}` | `{delivered: bool}` |
| `swarm_task_status` | Query task board | `{filter?: "pending"\|"assigned"\|"mine"}` | `{tasks: [{id, title, status, worker}]}` |
| `swarm_claim_file` | Claim advisory file lock | `{path}` | `{claimed: bool, held_by?: string}` |

### Extended operations (4.3)
| Tool | Description | Input | Output |
|------|-------------|-------|--------|
| `swarm_complete_task` | Mark assigned task done | `{resolution}` | `{completed: bool}` |
| `swarm_create_task` | Create a new task | `{title, description, target_worker?, priority?}` | `{task_id}` |
| `swarm_get_learnings` | Query learnings from completed tasks | `{query?: string}` | `{learnings: [{task, content}]}` |
| `swarm_report_progress` | Report structured progress | `{phase?, pct?, blockers?}` | `{ok: true}` |

## Message Types

| Type | Purpose | Example |
|------|---------|---------|
| `finding` | Share a discovery | "Auth middleware changed in middleware/auth.ts" |
| `warning` | Alert about a problem | "Shared types are broken after my last commit" |
| `dependency` | Signal a dependency | "I just pushed API changes — pull before continuing" |
| `status` | Update on progress | "API endpoint done, frontend can start" |
| `operator` | Message from the human operator | "Prioritize the login bug over the refactor" |

## Message Store Schema

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,        -- worker name or "operator"
    recipient TEXT NOT NULL,     -- worker name or "*" for broadcast
    msg_type TEXT NOT NULL,      -- finding, warning, dependency, status, operator
    content TEXT NOT NULL,
    created_at REAL NOT NULL,    -- Unix timestamp
    read_at REAL,                -- NULL = unread
    UNIQUE(sender, recipient, msg_type, content)  -- dedup within window
);
```

**Rate limiting**: Before INSERT, check if an identical (sender, recipient, msg_type) exists within the last 60 seconds. If so, update `created_at` instead of inserting.

## MCP Transport

The MCP server rides on the existing aiohttp daemon. As shipped, Swarm speaks both the current Streamable HTTP transport and the legacy SSE transport so any MCP-capable client works:

- **Streamable HTTP**: `POST /mcp`, `GET /mcp`, `DELETE /mcp`
- **Legacy SSE**: `GET /mcp/sse` (server→client stream) + `POST /mcp/message` (client→server JSON-RPC)
- **Auth**: `SWARM_MANAGED=1` header / env check on connect
- **Worker ID**: passed as `X-Swarm-Worker` header; all subsequent tool calls are attributed to that worker

Claude Code MCP config (registered in `~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "swarm": {
      "type": "sse",
      "url": "http://localhost:9090/mcp/sse",
      "headers": {
        "X-Swarm-Worker": "${SWARM_WORKER_NAME}"
      }
    }
  }
}
```

The holder sets `SWARM_WORKER_NAME` in the worker's env (alongside `SWARM_MANAGED=1`).

## System Prompt Additions

Add to CLAUDE.md or per-worker system prompt:
```
You are connected to the Swarm coordination system via MCP tools.
- Call swarm_check_messages() at the start of each task, after completing a task,
  and when you encounter unexpected file changes or merge conflicts.
- Use swarm_send_message() to notify other workers about breaking changes,
  shared discoveries, or dependency updates.
- Use swarm_complete_task() when you finish your assigned task.
- Use swarm_claim_file() before editing files that other workers might touch.
```

## Deliverables

### 4.1 — Message Store + API Endpoints (~4-6h)
- SQLite message store at `~/.swarm/messages.db`
- API endpoints: `POST /api/messages/send`, `GET /api/messages/{worker}`, `POST /api/messages/read`
- Rate limiting + dedup logic
- Operator send UI in dashboard (simple input per worker)
- Messages in buzz log with "message" category badge

**Files**: `src/swarm/messages/store.py` (new), `src/swarm/server/routes/messages.py` (new), dashboard.js, base.html

### 4.2 — MCP SSE Server + Core Tools (~8-10h)
- MCP SSE transport on `/mcp/sse` and `/mcp/message`
- Worker identification via `X-Swarm-Worker` header
- 4 core tools: `swarm_check_messages`, `swarm_send_message`, `swarm_task_status`, `swarm_claim_file`
- MCP server registration in `~/.claude/settings.json` via install.py
- `SWARM_WORKER_NAME` env var set by holder

**Files**: `src/swarm/mcp/server.py` (new), `src/swarm/mcp/tools.py` (new), `src/swarm/pty/holder.py`, `src/swarm/hooks/install.py`

### 4.3 — Extended MCP Tools (~4-6h)
- 4 additional tools: `swarm_complete_task`, `swarm_create_task`, `swarm_get_learnings`, `swarm_report_progress`
- Progress tracking field on Worker (phase, pct, blockers)
- Learnings query by keyword/area

**Files**: `src/swarm/mcp/tools.py`, `src/swarm/worker/worker.py`, `src/swarm/tasks/task.py`

### 4.4 — Dashboard + Operator Integration (~3-4h)
- Message send input in worker detail panel
- Message history view in buzz log
- Operator messages delivered via same channel
- MCP connection status indicator per worker

**Files**: `dashboard.js`, `base.html`, `templates/partials/system_log.html`

## Total Effort: ~19-26 hours

## What This Replaces

| Current Pattern | Replaced By | Fallback |
|----------------|-------------|----------|
| `cross_task_hook.sh` (Write file → curl) | `swarm_create_task` MCP tool | Hook still works |
| `complete_task_hook.sh` (Write file → curl) | `swarm_complete_task` MCP tool | Hook still works |
| Queen relaying discoveries | `swarm_send_message` direct | Queen still available |
| PTY injection for operator messages | Operator → `swarm_check_messages` | Terminal still works |
| Inferring progress from PTY output | `swarm_report_progress` | PTY monitoring unchanged |

## What This Does NOT Replace

- PreToolUse approval hooks (synchronous interception — MCP can't do this)
- SessionEnd hooks (lifecycle events — daemon needs push notification)
- PreCompact/PostCompact hooks (same — lifecycle push events)
- PTY-based state detection (still needed for BUZZING/RESTING/WAITING classification)
