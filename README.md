# Swarm

A hive-mind orchestrator for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents. Run multiple agents in parallel across your projects, with an auto-pilot that handles approvals, revives crashed workers, and a Queen conductor for coordination.

Workers live in tmux panes. A Textual TUI (or web dashboard) gives you a single view of the whole hive. The **Buzz** auto-pilot watches every pane and handles routine decisions automatically. The **Queen** (a headless Claude instance) steps in for complex coordination.

## Features

- **Multi-agent orchestration** -- launch Claude Code in parallel tmux panes, one per project
- **Auto-pilot (Buzz)** -- automatically approves prompts, revives crashed agents, escalates when stuck
- **Queen conductor** -- headless Claude that coordinates across workers, assigns tasks, resolves conflicts
- **Task board** -- create, assign, and track tasks across workers with dependency support
- **Adaptive polling** -- exponential backoff when idle, circuit breakers for dead workers
- **TUI dashboard** -- real-time Textual app with worker list, detail view, buzz log, and task panel
- **Web dashboard** -- browser-based UI served via aiohttp on `:8080`
- **Daemon mode** -- REST + WebSocket API for headless operation
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

# 2. Launch all workers in tmux
swarm launch all

# 3. Open the TUI dashboard
swarm tui
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `swarm init` | Discover projects and generate `swarm.yaml` |
| `swarm launch <group>` | Start workers in tmux (by group name, worker name, or `-a` for all) |
| `swarm tui [session]` | Open the Textual TUI dashboard |
| `swarm serve` | Serve the web dashboard on `:8080` |
| `swarm daemon` | Run as a background daemon with REST + WebSocket API |
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

buzz:
  poll_interval: 5.0             # base polling interval (seconds)
  auto_approve_yn: false         # auto-approve Y/N prompts
  max_revive_attempts: 3         # crash-loop revives before giving up
  escalation_threshold: 15.0     # seconds before escalating to Queen
  max_poll_failures: 5           # circuit breaker threshold
  max_idle_interval: 30.0        # max backoff when idle (seconds)
  auto_stop_on_complete: true    # stop pilot when all tasks done

queen:
  cooldown: 30.0                 # min seconds between Queen API calls
  enabled: true

notifications:
  terminal_bell: true
  desktop: true
  debounce_seconds: 5.0
```

See [`swarm.yaml.example`](swarm.yaml.example) for the full reference.

## Architecture

```
┌─────────────────────────────────────────────┐
│  TUI / Web Dashboard                        │
├─────────────────────────────────────────────┤
│  Buzz Auto-Pilot        Queen Conductor     │
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
2. **Buzz** -- auto-pilot loop that polls panes, auto-approves, revives, and escalates
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
