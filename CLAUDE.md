# Swarm — Project Guide

> See `~/.claude/CLAUDE.md` for universal rules (design principles, code quality, TDD workflow, quality gates).

## 1. Quick Reference

### Essential Rules
| Rule | Action |
|------|--------|
| Before commit | Use `/commit` slash command |
| Pre-commit validation | Use `/check` slash command |
| Bug fix | Use `/fix-and-ship` or `/diagnose` first |
| Test failures | STOP — fix before continuing |
| Warnings | STOP — warnings = failures |
| `type: ignore` | FORBIDDEN — fix the type error |
| Creating a file | SEARCH existing code first |
| Installed tool stale? | `uv tool uninstall swarm-ai && uv cache clean swarm-ai && uv tool install --no-cache .` |

### Key Files
| File | When to Check |
|------|---------------|
| `swarm.yaml` | Configuring workers, drones, queen, groups |
| `src/swarm/worker/state.py` | Debugging state detection issues |
| `src/swarm/drones/pilot.py` | Understanding the poll loop and drone actions |
| `src/swarm/server/daemon.py` | Core daemon lifecycle, events, WebSocket broadcasts |
| `src/swarm/server/api.py` | All HTTP/WebSocket endpoints |
| `src/swarm/web/templates/dashboard.html` | Dashboard UI and JS |

---

## 2. What This Is

A Python web tool for orchestrating multiple Claude Code agents.
Workers run in PTYs managed by a pty-holder sidecar. The background drones handle routine decisions.
The Queen (headless `claude -p`) handles complex decisions.

### Architecture
- **Package**: `src/swarm/` — installable via `uv tool install` or `pipx`
- **CLI**: `swarm` with subcommands: `start`, `launch`, `serve`, `daemon`, `status`, `send`, `kill`, `tasks`, `tunnel`, `test`, `validate`, `check-states`, `init`, `install-hooks`, `web`
- **Layers**: Hooks (per-worker) → Drones (background workers) → Queen (conductor)
- **Web**: aiohttp server + WebSocket push + Jinja2 dashboard

### Key Modules
- `cli.py` — Click CLI entry point
- `config.py` — YAML config loader (swarm.yaml)
- `pty/` — PTY holder, process management, ring buffer, WS bridge (holder.py, process.py, pool.py, buffer.py, bridge.py)
- `worker/` — Worker dataclass, state detection, lifecycle (worker.py, state.py, manager.py)
- `drones/` — Background drone loop, decision rules, action log (pilot.py, rules.py, log.py)
- `queen/` — Headless Claude conductor (queen.py, session.py)
- `hooks/` — Claude Code hook installer (install.py)
- `server/` — Daemon, API routes, WebSocket, proposals, task manager
- `tasks/` — Task board, history, proposals, workflows
- `web/` — Dashboard templates and static assets

---

## 3. Design Principles

### Architecture Guidelines
- **Event-driven decoupling** — Pilot emits events, daemon subscribes; never tight-couple components
- **Feature-based modules** — Organize by domain (worker/, drones/, queen/, tasks/), not by layer
- **Async everywhere** — All PTY/holder calls are async; all I/O is async. Never block the event loop.
- **Explicit types** — Use dataclasses and type hints; help AI and humans understand intent
- **Thin API handlers** — Validation in handlers, business logic in daemon/pilot/managers

---

## 4. Conventions

### State Machine
- `BUZZING` — worker is actively processing ("esc to interrupt" visible)
- `RESTING` — worker is idle (prompt visible, < 5 min)
- `SLEEPING` — worker idle > 5 min (display-only state)
- `WAITING` — worker showing a choice/approval prompt
- `STUNG` — worker's Claude process has exited

### PTY Integration
- Output read from in-process ring buffer via `worker.process.get_content()`
- Input sent via `worker.process.send_keys()` / `send_enter()` / `send_interrupt()`
- Worker state stored in Worker objects (no external state)
- Never inject text into worker PTYs while the user may be typing

### Polling & Lifecycle
- Throttle polling with adaptive backoff (5s base → 15s max)
- Never run idle polling loops without a shutdown mechanism
- All async tasks must have `try/except BaseException` to catch `CancelledError`
- Use watchdog patterns for critical background loops

---

## 5. Critical Rules

After making code edits, always run `uv run ruff format` before validation checks. Never commit unformatted code.

### Post-Change Validation (MANDATORY)
After making code changes, run `/check` and show the output. Do NOT report the task as complete until all checks pass with zero errors and zero warnings. If anything fails, fix it and re-run.

### Key Triggers
```yaml
IF test_fails        → STOP: Fix test before continuing
IF creating_file     → STOP: Search existing code first
IF iteration>2 && no_progress → RESET: Verify assumptions with tools
IF process_error     → CHECK: Holder running? Worker alive? ProcessError details?
IF state_not_updating → CHECK: Pilot loop alive? get_content() output? classify_worker_output?
IF code_change_not_working → CHECK: Using dev version (uv run) or installed tool?
IF command_fails     → FIX: Read error, fix syntax, retry (3x). Don't give up.
IF asked_to_verify   → ACTUALLY_CHECK: Run the command. Never assume.
```

### Command Failures — Be Persistent!
```
Command fails? → Read error, fix syntax, retry. Don't give up.
Need to verify? → Actually run the query/curl/command. Never assume.
Pattern: Try → Fix → Retry (3x) → Then ask user with details of attempts.
TDD Bug Fix: Write test (red) → Fix → Run test → Iterate (5x) → Ask if stuck.
```

---

## 6. Workflow

### Bug Fix Sequence
1. Reproduce the bug (or understand the report)
2. Use `/diagnose` to trace the full data flow
3. Write failing regression test — confirm it **fails** (red). If it passes, re-diagnose.
4. TDD loop — implement fix, run specific test (`uv run pytest tests/test_foo.py::test_name -q`), iterate until green (max 5 iterations, ask if 3x same error)
5. Run `/check` (format + lint + full test suite)
6. Document root cause in commit message

### Feature Sequence
1. Search existing code first
2. Design types/dataclasses
3. Write tests
4. Implement (tests should fail initially)
5. Iterate until all tests pass
6. Run `/check`

---

## 7. Slash Commands

**IMPORTANT**: Use these instead of running commands manually. They handle error cases and ensure consistency.

| Command | Purpose | When to Use |
|---------|---------|-------------|
| `/check` | Run pre-commit validation (ruff format + lint + pytest) | Before committing, during development |
| `/commit` | Create a git commit following conventions | When ready to commit changes |
| `/diagnose` | Trace full data flow before fixing a bug | Before any bug fix — prevents partial fixes |
| `/fix-and-ship` | Autonomous bug fix pipeline (diagnose → TDD → validate → commit) | End-to-end bug fix with one approval gate |
| `/get-latest` | Pull latest from origin/main and merge | Before starting work, after conflicts |
| `/interview` | Deep-dive requirements interview for a feature | Before building complex features |

### Command Details
- **`/check`**: Runs ruff format, ruff check, pytest. Must pass with zero warnings.
- **`/commit`**: Formats, lints, tests, drafts message, commits, optionally pushes. Run `/check` first.
- **`/diagnose`**: Maps complete architecture path before fixing. Prevents whack-a-mole debugging.
- **`/fix-and-ship`**: Full pipeline: diagnose → regression test (TDD) → fix → validate → commit + push.

```yaml
# ALWAYS use slash commands for these operations:
PRE_COMMIT: /check (not manual uv run ruff/pytest)
COMMITTING: /commit (not manual git commit)
BUG_FIXING: /fix-and-ship or /diagnose first
```

---

## 8. Development

### Standard Commands
```bash
uv sync                      # Install dependencies
uv run swarm --help          # Run CLI (dev version)
uv run swarm serve           # Web mode on :9090
uv run swarm start [target]  # Launch workers + web dashboard + open browser
uv run swarm status              # One-shot status of all workers
uv run swarm check-states        # Diagnostic: stored vs fresh state
uv run swarm tunnel              # Cloudflare tunnel for remote access
uv run swarm test                # Supervised orchestration test
uv run swarm validate            # Validate swarm.yaml
uv run ruff format src/ tests/  # Format code
uv run ruff check src/ tests/   # Lint code
uv run pytest tests/ -q         # Run tests
```

### Dev vs Installed Version
`swarm` at `~/.local/bin/swarm` is the **installed** (potentially stale) version.
`uv run swarm` uses the **dev** version from the project .venv.

After changing source code, reinstall with cache-busting:
```bash
uv tool uninstall swarm-ai && uv cache clean swarm-ai && uv tool install --no-cache /home/bschleifer/projects/swarm
```
**WARNING**: `uv tool install --force` is NOT enough — uv reuses its build cache.

---

## 9. Swarm / Conductor

### Headless Conductor Pattern
Instead of infinite polling loops, use bounded headless invocations with clear exit conditions:
```bash
claude -p "Check swarm agent status. If any agent needs approval, approve it. If any agent is idle and there are pending tasks, assign one. If all agents are idle and no tasks remain, output SWARM_COMPLETE." \
  --allowedTools "Bash,Read" --max-turns 10
```
Wrap in a bash loop with proper sleep and exit detection:
```bash
while true; do
  OUTPUT=$(claude -p "..." --allowedTools "Bash,Read" --max-turns 10 2>&1)
  echo "$OUTPUT" >> swarm-conductor.log
  if echo "$OUTPUT" | grep -q "SWARM_COMPLETE"; then
    echo "All agents idle, no tasks remain. Exiting."
    break
  fi
  sleep 30
done
```
Key rules: always set `--max-turns`, always define an exit signal, always log output, always sleep between cycles.
