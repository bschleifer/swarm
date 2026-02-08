# Swarm

A hive-mind orchestrator for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents. Run multiple agents in parallel across your projects, with background drones that handle approvals, revive crashed workers, and a Queen conductor for coordination.

Workers live in tmux panes. A Textual TUI (or web dashboard) gives you a single view of the whole hive. **Drones** watch every pane and handle routine decisions automatically. The **Queen** (a headless Claude instance) steps in for complex coordination.

## Requirements

- Python 3.12+
- [tmux](https://github.com/tmux/tmux) >= 3.2
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI (`claude`)
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
# Install as a CLI tool
uv tool install git+https://github.com/bschleifer/swarm.git

# Or clone and install from source
git clone https://github.com/bschleifer/swarm.git
cd swarm
uv tool install .
```

Then run the setup wizard:

```bash
swarm init
```

This does three things:
1. **Checks tmux** -- verifies it's installed and >= 3.2
2. **Configures tmux** -- writes a swarm block to `~/.tmux.conf` (mouse support, pane borders, click-to-swap)
3. **Installs Claude Code hooks** -- auto-approves safe tools (Read, Edit, Write, Glob, Grep) so workers don't stall on every file access
4. **Generates config** -- scans `~/projects` for git repos, lets you pick workers and define groups, writes to `~/.config/swarm/config.yaml`

## Quick Start

```bash
# Launch a group and open the TUI in one command
swarm tui default

# Or launch all workers
swarm tui all
```

That's it. `swarm tui <group>` auto-launches the workers if they aren't already running, then opens the dashboard. Run it again and it reconnects to the existing session without re-launching.

You can also launch and manage separately:

```bash
swarm launch all    # start workers in tmux (headless)
swarm tui           # open the TUI (auto-discovers running session)
swarm status        # one-shot status check from the CLI
```

## Features

- **Multi-agent orchestration** -- launch Claude Code in parallel tmux panes, one per project
- **Background drones** -- automatically approve prompts, revive crashed agents, escalate when stuck
- **Queen conductor** -- headless Claude that coordinates across workers, assigns tasks, resolves conflicts
- **Task board** -- create, assign, and track tasks across workers with dependency support
- **Adaptive polling** -- exponential backoff when idle, circuit breakers for dead workers
- **TUI dashboard** -- real-time Textual app with worker list, detail view, drone log, and task panel
- **Web dashboard** -- browser-based UI on `:8080`, toggleable from the TUI
- **Config editor** -- edit all settings from the TUI or web with live hot-reload
- **Live worker management** -- add and remove workers at runtime without restarting
- **Notifications** -- terminal bell and desktop notifications when workers need attention
- **YAML config** -- declarative config with workers, groups, and tuning knobs

## The TUI

Running `swarm tui` opens the Textual dashboard. The main view shows the worker list, a detail pane with the selected worker's tmux output, a task panel, and a drone log.

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Alt+B` | Toggle background drones on/off |
| `Alt+C` | Continue the selected worker (send Enter) |
| `Alt+A` | Continue all idle workers |
| `Alt+M` | Send a message to the selected worker |
| `Alt+E` | Send Escape to the selected worker |
| `Alt+K` | Kill the selected worker's pane |
| `Alt+R` | Revive a crashed worker |
| `Alt+X` | Quit |

### Command Palette (`Ctrl+P`)

- **Launch brood** -- start workers from a group or pick individually
- **Config** (`Alt+O`) -- open the config editor
- **Toggle web dashboard** (`Alt+W`) -- start/stop the browser UI
- **Toggle drones** (`Alt+B`) -- enable/disable background drones
- **Ask Queen** (`Alt+Q`) -- run Queen analysis on the selected worker
- **Create task** (`Alt+N`) -- add a task to the board
- **Assign task** (`Alt+D`) -- assign a task to the selected worker
- **Attach tmux** (`Alt+T`) -- attach to the selected worker's tmux pane
- **Screenshot** (`Alt+S`) -- save a screenshot of the TUI

## Web Dashboard

The web dashboard mirrors the TUI in a browser on `:8080`.

**From the TUI:** press `Alt+W` to toggle it on or off.

**From the CLI:**

```bash
swarm web start          # start in background
swarm web stop           # stop
swarm web status         # check if running
swarm serve              # run in foreground (blocking)
```

If `api_password` is set in the config (or `SWARM_API_PASSWORD` env var), config mutations require authentication.

## CLI Reference

| Command | Description |
|---------|-------------|
| `swarm` | Open the TUI (default) |
| `swarm init` | Set up tmux, hooks, and generate config |
| `swarm tui <target>` | Launch workers and open TUI (group, worker, or session name) |
| `swarm launch <target>` | Start workers in tmux (group name, worker name, number, or `-a`) |
| `swarm status` | One-shot status check of all workers |
| `swarm send <target> <msg>` | Send a message to a worker, group, or `all` |
| `swarm kill <worker>` | Kill a worker's tmux pane |
| `swarm tasks <action>` | Manage tasks (`list`, `create`, `assign`, `complete`) |
| `swarm serve` | Run web dashboard in foreground |
| `swarm web start\|stop\|status` | Manage web dashboard as background process |
| `swarm daemon` | Headless daemon with REST + WebSocket API |
| `swarm validate` | Validate config |
| `swarm install-hooks` | Install Claude Code auto-approval hooks |

## Configuration

`swarm init` generates config at `~/.config/swarm/config.yaml`. You can also create one manually:

```yaml
session_name: swarm
projects_dir: ~/projects

workers:
  - name: api
    path: ~/projects/api-server
  - name: web
    path: ~/projects/frontend
  - name: tests
    path: ~/projects/test-suite

groups:
  - name: default
    workers: [api, web]
  - name: all
    workers: [api, web, tests]

drones:
  poll_interval: 5.0             # seconds between polls
  auto_approve_yn: false         # auto-approve Y/N prompts
  max_revive_attempts: 3         # revives before giving up
  escalation_threshold: 15.0     # seconds idle before escalating to Queen
  max_poll_failures: 5           # consecutive failures before circuit breaker
  max_idle_interval: 30.0        # max backoff interval when idle
  auto_stop_on_complete: true    # stop drones when all tasks complete

queen:
  cooldown: 30.0                 # min seconds between Queen invocations
  enabled: true

notifications:
  terminal_bell: true
  desktop: true
  debounce_seconds: 5.0

# Optional: password-protect web dashboard config mutations
# api_password: "your-secret"
```

Config is loaded from (first match wins):
1. Explicit `-c /path/to/config.yaml`
2. `./swarm.yaml` in the current directory
3. `~/.config/swarm/config.yaml`

All settings can be edited live from the TUI (`Alt+O`) or the web dashboard (`/config`).

## Architecture

```
┌─────────────────────────────────────────────┐
│  TUI / Web Dashboard                        │
├─────────────────────────────────────────────┤
│  Background Drones      Queen Conductor     │
│  (poll → decide → act)  (headless claude)   │
├─────────────────────────────────────────────┤
│  Task Board             Notification Bus    │
├─────────────────────────────────────────────┤
│  tmux session                               │
│  ┌────────┐ ┌────────┐ ┌────────┐          │
│  │ worker │ │ worker │ │ worker │  ...      │
│  │  api   │ │  web   │ │ tests  │          │
│  └────────┘ └────────┘ └────────┘          │
└─────────────────────────────────────────────┘
```

**Worker states:**
- **BUZZING** -- actively working (Claude is processing)
- **RESTING** -- idle, waiting for input
- **STUNG** -- exited or crashed

**Decision layers:**
1. **Hooks** -- per-worker Claude Code hooks for instant tool approvals
2. **Drones** -- background polling that auto-approves, revives, and escalates
3. **Queen** -- headless Claude for cross-worker coordination and task assignment

## Development

```bash
git clone https://github.com/bschleifer/swarm.git
cd swarm
uv sync                    # install dependencies
uv run swarm --help        # run CLI from source
uv run pytest tests/ -q    # run test suite
uv run ruff check src/     # linting
uv run ruff format src/    # formatting
```

## License

MIT
