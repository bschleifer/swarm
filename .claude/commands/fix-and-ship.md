---
description: Autonomous bug fix pipeline — diagnose, fix with TDD, validate, commit (one human approval gate)
---

Autonomously fix the bug described below and ship it with one human approval gate before pushing.

**Bug description**: $ARGUMENTS

---

## Phase 1: Diagnose

Trace the full data flow to identify root cause. Do NOT stop for confirmation — proceed autonomously.

### 1a. Trace the Complete Data Flow

1. **User Action** — What triggers the bug? (CLI command, TUI keybinding, Web UI click)
2. **Entry Point** — Which module handles it? (`cli.py`, `ui/app.py`, `web/app.py`, `server/api.py`)
3. **Config** — Does `config.py` / `swarm.yaml` affect the behavior?
4. **Tmux Layer** — `tmux/hive.py`, `tmux/cell.py`, `tmux/layout.py`
5. **Worker Layer** — `worker/worker.py`, `worker/state.py`, `worker/manager.py`
6. **Drones** — `drones/pilot.py`, `drones/rules.py`, `drones/log.py`
7. **Queen** — `queen/queen.py`, `queen/session.py`
8. **Tasks** — `tasks/task.py`, `tasks/board.py`

### 1b. Map ALL Affected Code Paths

Use Grep and Glob to find ALL references. Check for:
- Multiple callers of the same function
- Shared dataclasses that other modules depend on
- Callbacks/event listeners (`on_change`, `on_entry`)
- UI widgets that render the same data (TUI and Web)

### 1c. Identify the Bug

- **Root cause**: Where does the data first go wrong?
- **Propagation**: Which downstream paths are affected?
- **Symptoms**: Why does the user see the behavior they reported?

---

## Phase 2: Fix (TDD-First)

Write a failing regression test, then iterate the fix until green. Proceed autonomously.

### Rules
```
BUG_FIX_MODE:
  - EXACT_ISSUE_ONLY (no refactoring)
  - MINIMAL_CHANGE (don't touch adjacent code)
  - TEST_FIRST mandatory — NEVER implement fix before writing regression test
  - TDD_LOOP: red → fix → run test → iterate (5x max, ask if 3x same error)
```

### 2a. Write Regression Test FIRST (Red)

1. Write a regression test in the appropriate `tests/test_*.py` file
2. Run the test to confirm it **fails** (red):
   ```bash
   uv run pytest tests/<test_file>.py -x -q -k "<test_name>"
   ```
3. **If the test passes** → the diagnosis is wrong. Go back to Phase 1.

### 2b. TDD Inner Loop (max 5 iterations)

```
ITERATION = 1
LAST_ERROR = ""
SAME_ERROR_COUNT = 0

WHILE ITERATION <= 5:
  1. Implement or refine the minimal fix at the root cause
  2. Run the specific test:
     uv run pytest tests/<test_file>.py -x -q -k "<test_name>"
  3. IF passes (green) → proceed to 2c
  4. IF fails:
     a. Read the assertion error
     b. IF same error as LAST_ERROR:
        SAME_ERROR_COUNT += 1
        IF SAME_ERROR_COUNT >= 3 → HARD STOP
     c. ELSE:
        SAME_ERROR_COUNT = 1
        LAST_ERROR = current error
     d. ITERATION += 1

IF ITERATION > 5 → HARD STOP
```

### On HARD STOP (5 iterations or 3x same error)

STOP immediately. Report to the user:

```
TDD LOOP FAILED — [reason]
──────────────────────────
Iteration 1: [what was tried, what failed]
...
Iteration N: [what was tried, what failed]

Requesting manual intervention.
```

Use AskUserQuestion:
- "Show me the errors — I'll guide you"
- "Try a different approach"
- "Abort pipeline"

### 2c. Post-Fix Cleanup

1. Fix any downstream paths that were affected
2. Update related dataclasses/types if they changed
3. Run `uvx ruff format .` on modified files

---

## Phase 3: Validate (Self-Healing Loop)

Run full validation and auto-fix failures — up to 3 iterations.

### Loop (max 3 iterations)

```
ITERATION = 1

WHILE ITERATION <= 3:
  Run: uvx ruff check . && uvx ruff format --check . && uv run pytest tests/ -x -q
  IF passes → proceed to Phase 4
  IF fails:
    1. Identify which check failed (ruff lint / ruff format / pytest)
    2. Read the specific error output
    3. Fix the issue
    4. ITERATION += 1

IF ITERATION > 3 → HARD STOP
```

### On HARD STOP (3 failures)

STOP and report to user. Use AskUserQuestion:
- "Show me the errors — I'll guide you"
- "Revert and start over"
- "Abort pipeline"

---

## Phase 4: Commit & Push (HUMAN GATE)

Present a full pipeline report and ask for explicit push approval.

### Pipeline Report

Print this report in your response (as text, NOT bash commands):

```
═══════════════════════════════════════════════════════════════
FIX-AND-SHIP PIPELINE REPORT
═══════════════════════════════════════════════════════════════

Bug Description:
  $ARGUMENTS

Root Cause:
  [What was wrong and where]

Fix Applied:
  [What was changed]

Files Modified:
  [List all files with A/M/D status]

TDD Loop:
  Iterations: [N/5]
  Result: GREEN

Validation:
  Iterations: [N/3]
  Result: PASSED

Diff Summary:
  [git diff --stat of the fix]

═══════════════════════════════════════════════════════════════
```

### Approval Gate

Use AskUserQuestion: **"All checks pass. Push to main?"**

Options:
- **"Yes — commit and push"** — Proceed
- **"Commit locally only"** — Commit but don't push
- **"Abort"** — Stop here

### On Approval

```bash
git add -A
git commit -m "$(cat <<'EOF'
fix: <concise description>

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

If push was approved:
```bash
git push origin main
```

---

## Safety Rules (Global)

- NEVER push without explicit user approval (Phase 4 gate)
- NEVER commit .env or credentials
- NEVER use --amend, --no-verify, or --force-push
- NEVER skip the validation loop (Phase 3)
- After 3 failed validation iterations → STOP and ask user
- NEVER implement fix before writing regression test (TDD-first is mandatory)
- BUG_FIX_MODE: exact issue only, minimal change, regression test required
