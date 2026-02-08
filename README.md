# Swarm

A hive-mind orchestrator for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents. Run multiple agents in parallel across your projects, with background drones that handle approvals, revive crashed workers, and a Queen conductor for coordination.

Workers live in tmux panes. A Textual TUI (or web dashboard) gives you a single view of the whole hive. **Drones** watch every pane and handle routine decisions automatically. The **Queen** (a headless Claude instance) steps in for complex coordination.

## Features

- **Multi-agent orchestration** -- launch Claude Code in parallel tmux panes, one per project
- **Background drones** -- automatically approve prompts, revive crashed agents, escalate when stuck
- **Queen conductor** -- headless Claude that coordinates across workers, assigns tasks, resolves conflicts
- **Task board** -- create, assign, and track tasks across workers with dependency support
- **Adaptive polling** -- exponential backoff when idle, circuit breakers for dead workers
- **TUI dashboard** -- real-time Textual app with worker list, detail view, drone log, and task panel
- **Web dashboard** -- browser-based UI served via aiohttp on `:8080`, toggleable from the TUI
- **Config editor** -- edit all settings from the TUI or web dashboard with live hot-reload
- **Live worker management** -- add and remove workers at runtime without restarting
- **Notifications** -- terminal bell and desktop notifications when workers need attention
- **YAML config** -- declarative `swarm.yaml` with workers, groups, and tuning knobs

## Requirements

- Python 3.12+
- [tmux](https://github.com/tmux/tmux)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI (`claude`)
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Installation

```bash
# Install as a tool (recommended)
uv tool install /path/to/swarm
# -- or from the repo directly --
uv tool install git+https://github.com/bschleifer/swarm.git

# Or install in a virtualenv for development
git clone https://github.com/bschleifer/swarm.git
cd swarm
uv sync
```

## Quick Start

```bash
# 1. Generate a config by scanning ~/projects for git repos
swarm init

# 2. Open the TUI
swarm
```

That's it. The TUI works without a pre-existing tmux session -- use the command palette (`Ctrl+P`) to **Launch brood** and select which workers or groups to start.

Or launch from the CLI first if you prefer:

```bash
swarm launch all    # start all workers in tmux
swarm               # then open the TUI
```

## The TUI

Running `swarm` (or `swarm tui`) opens the Textual dashboard. The main view shows the worker list, a detail pane with the selected worker's tmux output, and a drone log.

### Footer Shortcuts

These are always visible at the bottom:

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

### Command Palette

Press `Ctrl+P` to open the system command palette for less frequent actions:

- **Launch brood** -- start a tmux session with selected workers/groups
- **Config** (`Alt+O`) -- open the config editor modal
- **Toggle web dashboard** (`Alt+W`) -- start/stop the web UI
- **Toggle drones** (`Alt+B`) -- enable/disable background drones
- **Ask Queen** (`Alt+Q`) -- run Queen analysis on the selected worker
- **Create task** (`Alt+N`) -- add a task to the board
- **Assign task** (`Alt+D`) -- assign a task to the selected worker
- **Attach tmux** (`Alt+T`) -- attach to the selected worker's tmux pane
- **Screenshot** (`Alt+S`) -- save a screenshot of the TUI

### Config Editor

Open with `Alt+O` or via the command palette. Five tabs:

- **Drones** -- poll interval, escalation threshold, auto-approve, revive limits, circuit breaker settings
- **Queen** -- cooldown, enabled toggle
- **Notifications** -- terminal bell, desktop notifications, debounce
- **Workers** -- add/remove workers with path validation
- **Groups** -- manage worker groups

Changes are saved to `swarm.yaml` and hot-reloaded immediately -- no restart needed.

## Web Dashboard

The web dashboard runs on `:8080` and mirrors the TUI's functionality in a browser.

**From the TUI:** press `Alt+W` to toggle it on or off.

**From the CLI:**

```bash
swarm web start          # start in background
swarm web stop           # stop
swarm web status         # check if running
swarm serve              # run in foreground (blocking)
```

The web dashboard includes a config editor page at `/config` with the same capabilities as the TUI modal. If `api_password` is set in the config (or `SWARM_API_PASSWORD` env var), mutating config endpoints require authentication.

## CLI Commands

| Command | Description |
|---------|-------------|
| `swarm` | Open the TUI dashboard (default when no subcommand given) |
| `swarm init` | Discover projects and generate `swarm.yaml` |
| `swarm launch <target>` | Start workers in tmux (group name, worker name, or `-a` for all) |
| `swarm tui [session]` | Open the TUI dashboard (same as bare `swarm`) |
| `swarm serve` | Run the web dashboard in the foreground on `:8080` |
| `swarm web start/stop/status` | Manage the web dashboard as a background process |
| `swarm daemon` | Run as a headless daemon with REST + WebSocket API |
| `swarm status` | One-shot status check of all workers |
| `swarm send <target> <msg>` | Send a message to a worker, group, or `all` |
| `swarm kill <worker>` | Kill a worker's tmux pane |
| `swarm tasks <action>` | Manage the task board (`list`, `create`, `assign`, `complete`) |
| `swarm validate` | Validate `swarm.yaml` |
| `swarm install-hooks` | Install Claude Code auto-approval hooks |

## Configuration

Create a `swarm.yaml` in your project root (or run `swarm init` to generate one):

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
  - name: fullstack
    workers: [api, web]
  - name: all
    workers: [api, web, tests]

drones:
  poll_interval: 5.0             # seconds between polls
  auto_approve_yn: false         # auto-approve Y/N prompts
  max_revive_attempts: 3         # revives before giving up
  escalation_threshold: 15.0     # seconds idle before escalating to Queen
  max_poll_failures: 5           # consecutive failures before circuit breaker trips
  max_idle_interval: 30.0        # max backoff interval when idle
  auto_stop_on_complete: true    # stop drones when all tasks complete

queen:
  cooldown: 30.0                 # min seconds between Queen invocations
  enabled: true

notifications:
  terminal_bell: true
  desktop: true
  debounce_seconds: 5.0

# Optional: password-protect config mutations on the web dashboard
# api_password: "your-secret"
```

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
1. **Hooks** -- per-worker Claude Code hooks for instant approvals
2. **Drones** -- background polling loop that auto-approves, revives, and escalates
3. **Queen** -- headless Claude instance for cross-worker coordination and task assignment

## Development

```bash
uv sync                    # install dependencies
uv run swarm --help        # run CLI from source
uv run pytest tests/ -v    # run test suite
uv run mypy src/           # type checking
uv run ruff check src/     # linting
```

## License

MIT
