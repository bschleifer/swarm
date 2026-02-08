---
description: Trace full data flow before fixing a bug — prevents partial fixes
---

Investigate the bug described by the user: $ARGUMENTS

## Phase 1: Trace the Complete Data Flow

Before writing ANY fix, trace the full path through swarm's architecture:

1. **User Action** — What does the user do to trigger the bug? (CLI command, TUI keybinding, Web UI click, etc.)
2. **Entry Point** — Which module handles the action?
   - CLI: `cli.py` → Click command
   - TUI: `ui/app.py` → Textual action/keybinding
   - Web: `web/app.py` or `server/api.py` → aiohttp route handler
3. **Config** — Does `config.py` or `swarm.yaml` affect the behavior?
4. **Tmux Layer** — Does the action reach tmux? Trace through:
   - `tmux/hive.py` — Session/window management
   - `tmux/cell.py` — Pane operations (capture, send-keys, interrupt)
   - `tmux/layout.py` — Pane arrangement
5. **Worker Layer** — Does it involve worker state?
   - `worker/worker.py` — Worker dataclass
   - `worker/state.py` — State detection (BUZZING/RESTING/STUNG)
   - `worker/manager.py` — Worker lifecycle
6. **Drones** — Does the drone pilot make decisions here?
   - `drones/pilot.py` — Poll loop, coordination cycle
   - `drones/rules.py` — Decision rules
   - `drones/log.py` — Action logging
7. **Queen** — Does it escalate to the queen?
   - `queen/queen.py` — Headless Claude analysis
   - `queen/session.py` — Session management
8. **Tasks** — Does it involve the task board?
   - `tasks/task.py` — Task model
   - `tasks/board.py` — Task operations

## Phase 2: Map ALL Affected Code Paths

List every file and function involved. Use Grep and Glob to find ALL references. Check for:

- Multiple callers of the same function
- Shared dataclasses or types that other modules depend on
- Callbacks or event listeners (e.g., `on_change`, `on_entry`)
- Tmux pane user options (`@swarm_name`, `@swarm_state`) that may be stale
- UI widgets that render the same data (TUI and Web)

Present a summary:

```
DATA FLOW MAP
─────────────
Entry:     [file:line_number]
Config:    [file:line_number]
Tmux:      [file:line_number]
Worker:    [file:line_number]
Drones:    [file:line_number]
Queen:     [file:line_number]
Tasks:     [file:line_number]
UI/Web:    [file:line_number]
```

## Phase 3: Identify the Bug Location(s)

Based on the trace, identify:
- **Root cause**: Where does the data first go wrong?
- **Propagation**: Which downstream paths are affected?
- **Symptoms**: Why does the user see the behavior they reported?

Explain the bug clearly, referencing specific files and line numbers.

## Phase 4: STOP — Wait for Confirmation

Present your findings and proposed fix to the user. Do NOT implement anything yet.

Ask:
> Here is the full data flow and where the bug occurs. My proposed fix is [describe]. This will touch [N] files: [list them]. Should I proceed?

## Phase 5: Fix Across ALL Affected Paths

Only after user confirmation:
1. Fix the root cause
2. Fix any downstream paths that were affected
3. Update related types/dataclasses if they changed
4. Write a regression test that covers the specific bug scenario
5. Run `/check` to validate everything passes
