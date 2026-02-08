# Swarm — Project Guide

## What This Is
A Python + Textual TUI/web tool for orchestrating multiple Claude Code agents.
Workers run in tmux panes. The background drones handle routine decisions.
The Queen (headless `claude -p`) handles complex decisions.

## Architecture
- **Package**: `src/swarm/` — installable via `uv tool install` or `pipx`
- **CLI**: `swarm` with subcommands: `launch`, `tui`, `serve`, `status`, `install-hooks`
- **Layers**: Hooks (per-worker) → Drones (background workers) → Queen (conductor)

## Key Modules
- `cli.py` — Click CLI entry point
- `config.py` — YAML config loader (swarm.yaml)
- `tmux/` — Session/pane management (hive.py, cell.py, layout.py)
- `worker/` — Worker dataclass, state detection, lifecycle (worker.py, state.py, manager.py)
- `drones/` — Background drone loop, decision rules, action log (pilot.py, rules.py, log.py)
- `queen/` — Headless Claude conductor (queen.py, session.py)
- `hooks/` — Claude Code hook installer (install.py)
- `ui/` — Textual dashboard: app.py, worker_list.py, worker_detail.py, drone_log.py, modals

## Conventions
- All tmux calls use `asyncio.create_subprocess_exec`
- State detection: BUZZING (working), RESTING (idle), STUNG (exited)
- `capture-pane` for reading output, `send-keys` with `C-u` prefix for input
- Worker metadata stored as tmux pane user options (`@swarm_name`, `@swarm_state`)

## Swarm / Conductor

When working on the swarm/conductor system: never inject text into tmux panes while the user may be typing. Always use targeted send-keys with proper Enter key submission. Throttle polling to at least 30-second intervals. Never run idle polling loops without a shutdown mechanism.

### Headless Conductor Pattern
Instead of infinite polling loops, use bounded headless invocations with clear exit conditions:
```bash
claude -p "Check swarm agent status in tmux. If any agent needs approval, approve it. If any agent is idle and there are pending tasks, assign one. If all agents are idle and no tasks remain, output SWARM_COMPLETE." \
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

## General Rules

When the user shows a screenshot proving something is broken, do NOT claim it's correct. Trust the user's visual evidence over code assumptions, especially for UI color/theme rendering issues.

## Development
- `uv sync` — install dependencies
- `uv run swarm --help` — run CLI
- `uv run swarm tui` — launch dashboard
- `uv run swarm serve` — web mode on :8080
