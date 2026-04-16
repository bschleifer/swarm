# Multi-LLM Provider Support: Research & Architecture Reference

> **Purpose**: Standalone reference document for adding Gemini CLI and Codex CLI as
> alternative worker backends alongside Claude Code. Not an immediate implementation
> plan — a well-researched guide to return to when ready.

---

## 1. Executive Summary

Swarm's PTY transport layer is **already CLI-agnostic** — it spawns a command, reads bytes
from a ring buffer, and sends bytes back. The coupling to Claude Code lives entirely in the
**interpretation layer**: state detection regex patterns, drone approval logic, headless
invocations (`claude -p`), and auxiliary features (session tracking, hooks, slash commands).

Supporting Gemini CLI or Codex CLI requires:
1. **Extracting** Claude-specific logic into a provider abstraction (mechanical refactor)
2. **Implementing** per-provider state detection patterns (the hard part — each CLI has
   completely different terminal output for idle/busy/approval states)
3. **Adapting** headless invocation and approval response strategies per provider

The extraction refactor (Phase 1) delivers standalone value by cleaning up the codebase,
even if no other providers are added.

---

## 2. Current Claude Coupling: Complete Inventory

### 2.1 Worker Startup Command (4 locations — LOW effort)

| File | Line(s) | Code | Purpose |
|------|---------|------|---------|
| `src/swarm/worker/manager.py` | 23 | `command=["claude", "--continue"]` | Batch worker launch |
| `src/swarm/worker/manager.py` | 65 | `command = ["claude", "--continue"] if auto_start else ["bash"]` | Live worker add |
| `src/swarm/pty/holder.py` | 124 | `command = command or ["claude", "--continue"]` | Default fallback in PTY spawn |
| `src/swarm/pty/pool.py` | 183 | `return await self.spawn(name, cwd, command=["claude", "--continue"])` | Worker revive |

### 2.2 State Detection Patterns (22 references — HIGH effort)

All in `src/swarm/worker/state.py` — patterns like `_RE_PROMPT`, `_RE_CURSOR_OPTION`,
`_RE_HINTS`, `_RE_ACCEPT_EDITS`, `"esc to interrupt"`, `"? for shortcuts"`, etc.

### 2.3 Drone Auto-Approval Rules (1 key pattern — MEDIUM effort)

`src/swarm/drones/rules.py` — `_BUILTIN_SAFE_PATTERNS` matches Claude Code tool names.

### 2.4 Queen / Headless Invocations (30 references — HARD)

`src/swarm/queen/queen.py` — `["claude", "-p", ...]`, `--output-format json`,
`--resume`, session management, JSON envelope parsing.

### 2.5 Auxiliary Features (21 references — MEDIUM)

- `src/swarm/tasks/task.py` — `claude -p` for smart title generation
- `src/swarm/testing/report.py` — `claude -p` for AI-powered analysis
- `src/swarm/worker/usage.py` — `~/.claude/projects/` session JSONL
- `src/swarm/hooks/install.py` — `~/.claude/settings.json`
- `src/swarm/tasks/workflows.py` — Claude Code slash commands

---

## 3. Provider Comparison: CLI Terminal Behavior

### 3.1 Claude Code

| Aspect | Details |
|--------|---------|
| **Launch (interactive)** | `claude` or `claude --continue` |
| **Launch (headless)** | `claude -p "prompt" --output-format json\|text [--resume ID] [--max-turns N]` |
| **Busy indicator** | `"esc to interrupt"` |
| **Idle prompt** | `> ` or `❯ ` cursor, `"? for shortcuts"` |
| **Approval prompts** | Numbered choice menu: `> 1. Always allow` / `  2. Yes` / `  3. No` |
| **User questions** | `"Type something"` / `"Chat about this"` |
| **Accept edits** | `">> accept edits on (shift+tab to cycle)"` |
| **Interrupt** | Esc or Ctrl+C |
| **Session storage** | `~/.claude/projects/<encoded-path>/` JSONL |
| **JSON response** | `{"type": "result", "result": "...", "session_id": "..."}` |
| **Approval response** | Enter (selects highlighted option) |

### 3.2 Gemini CLI

| Aspect | Details |
|--------|---------|
| **Launch (interactive)** | `gemini` |
| **Launch (headless)** | `gemini -p "prompt" [--output-format json\|text]` |
| **Resume session** | `gemini --resume` or `gemini --resume <UUID>` |
| **Busy indicator** | `💬 ` emoji, `⠏` braille spinner, `"(esc to cancel, Xs)"` |
| **Idle prompt** | `gemini>` |
| **Approval prompts** | `"Approve? (y/n/always)"` |
| **Approval modes** | `--approval-mode default\|auto_edit\|yolo` or `--yolo` |
| **Approval response** | `y\r` (yes), `n\r` (no), `always\r` |
| **Tool names** | `run_shell_command`, `EditFile`, `FindFiles`, `ReadFile`, `WriteFile`, `SearchText`, `GoogleSearch`, `WebFetch` |
| **Known issues** | Orphaned processes at 100% CPU after terminal close |

### 3.3 Codex CLI (OpenAI)

| Aspect | Details |
|--------|---------|
| **Launch (interactive)** | `codex` (full-screen Ratatui TUI) |
| **Launch (headless)** | `codex exec "prompt" [--json]` (JSONL event stream) |
| **Busy indicator** | `▶` (Run), `▷` (Think) — Ratatui rendered |
| **Idle indicators** | `◇` (Idle), `□` (Free) |
| **Alternate screen** | **YES by default** — `--no-alt-screen` to disable |
| **Approval modes** | `--full-auto` / `-a on-request\|never\|untrusted` |
| **JSONL events** | `thread.started`, `turn.completed`, `item.completed` |
| **Key risk** | Alternate screen buffer makes PTY text detection unreliable |

---

## 4. Architecture

### 4.1 Provider Module Structure

```
src/swarm/providers/
├── __init__.py          # ProviderType enum, get_provider() factory
├── base.py              # Abstract base class (LLMProvider)
├── claude.py            # Claude Code provider (production)
├── gemini.py            # Gemini CLI provider (experimental)
├── codex.py             # Codex CLI provider (experimental)
├── opencode.py          # OpenCode provider (community, experimental)
├── generic.py           # Fallback provider used for custom CLIs defined via `custom_llms` in swarm.yaml
├── styled.py            # Wrapping provider that applies display/styling overrides (identity, color, label) on top of a base provider
├── tuned.py             # Wrapping provider that applies per-provider pattern overrides from `provider_overrides` in swarm.yaml
└── events.py            # Shared state-detection event types used by all providers
```

**Provider stack at a glance:**

- `base.py` defines the abstract contract every provider implements (launch command, state regexes, approval keys).
- `claude.py` / `gemini.py` / `codex.py` / `opencode.py` are the concrete CLI adapters.
- `generic.py` is used when a worker specifies a command that isn't one of the built-ins — driven by the `custom_llms` block in `swarm.yaml`.
- `styled.py` and `tuned.py` are *decorators*: they wrap a base provider with display and tuning overrides without duplicating core logic. See `provider_overrides` in the README for the YAML surface.
- `events.py` holds the event enum emitted by state detection (busy, idle, waiting, approval, etc.) so higher layers can stay provider-agnostic.

### 4.2 Config

```yaml
provider: claude  # Global default: "claude" | "gemini" | "codex"

workers:
  - name: worker-1
    path: ~/projects/foo
    # provider: claude  (inherits global default)
  - name: experiment
    path: ~/projects/bar
    provider: gemini   # Per-worker override
```

### 4.3 Claude-only config surfaces

A few config blocks are Claude-specific and ignored by other providers
because only the Claude Code CLI surfaces the corresponding mechanisms:

```yaml
# Opt-in Claude Code native sandbox. No-op for Gemini / Codex workers.
sandbox:
  enabled: true
  min_claude_version: "2.0"
  settings_overrides:
    allow_network: false
    denied_tools: ["Bash"]

test:
  # Pins the model identifier recorded in every swarm test run's
  # InfraSnapshot. Not a runtime override — only affects reporting.
  pin_model: claude-opus-4-7
```

---

## 5. Key Risks

1. **Codex Alternate Screen Buffer (HIGH)** — Ratatui renders to alternate buffer,
   making PTY text detection unreliable. May need `--no-alt-screen` or JSONL monitoring.
2. **Gemini Orphaned Processes (MEDIUM)** — Known bug with no SIGHUP handler.
3. **Codex Interactive Deadlocks (MEDIUM)** — Hangs on terminal prompts.
4. **Approval Semantics Mismatch** — Each provider has different approval UX.

---

## 6. Implementation Phases

- **Phase 1**: Extract Claude provider (refactor — no new functionality)
- **Phase 2**: Gemini CLI provider (requires empirical PTY capture)
- **Phase 3**: Codex CLI provider (requires alternate screen investigation)
- **Phase 4**: Provider-aware features (queen, tasks, usage tracking)
