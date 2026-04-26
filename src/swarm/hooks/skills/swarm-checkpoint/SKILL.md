---
name: swarm-checkpoint
description: Run the project's validation (/check), and on green stage + commit your changes using the project's /commit conventions; on red, report a blocker and note the Queen without committing. Use when the user asks to checkpoint, save progress, or commit safely mid-task.
user-invocable: true
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - mcp__swarm__swarm_report_progress
  - mcp__swarm__swarm_note_to_queen
---

# /swarm-checkpoint — Validate and commit progress

Use this skill to checkpoint your work mid-task. It runs the project's validation suite, and based on the result either commits cleanly OR reports a blocker without committing.

## Workflow

### Step 1 — Run validation

Invoke the project's pre-commit validation via the `/check` slash command. This runs format + lint + tests. ALL must pass with zero warnings.

If `/check` is not available in this project, run the equivalent: format + lint + test suite as defined in `CLAUDE.md` or the project manifest (`pyproject.toml`, `package.json`, `Makefile`, etc.).

### Step 2A — On PASS

1. Call `mcp__swarm__swarm_report_progress` with `phase="checkpointing"` and a reasonable percent estimate based on overall task progress.
2. Run `git status --short` and `git diff --stat` to identify exactly which files you intentionally changed.
3. Stage **only** the files you changed for this checkpoint, by name. **NEVER use `git add -A` or `git add .`** — that risks committing unintended files (`.env`, build artifacts, sibling work).
4. Generate a concise commit message following the project's conventions. Use the project's `/commit` slash command if present; otherwise commit manually with a HEREDOC body following Conventional Commits unless `CLAUDE.md` says otherwise.
5. Make the commit. Do **NOT** push unless the user has explicitly asked you to.
6. Report a one-line confirmation, e.g. `Checkpoint committed: <short hash> "<subject>".`

### Step 2B — On FAIL

1. Call `mcp__swarm__swarm_report_progress` with `phase="blocked"`, your current percent estimate, and `blockers` set to a brief description of what's failing (e.g., `lint: 3 errors`, `tests: 2 failing in test_foo.py`).
2. Call `mcp__swarm__swarm_note_to_queen` with the failure output (truncated to ~600 chars) so the Queen can see what's blocking.
3. **STOP.** Do not commit. Do not auto-create a fix-up task. Do not start a partial fix unless the user explicitly asks.
4. Report a one-line summary, e.g. `Checkpoint blocked: <reason>. Note sent to Queen.`

The worker stays on the original task. The next operator or worker prompt continues with the fix.

## Constraints

- Commits are atomic to this checkpoint — only files you intentionally changed for the work being saved.
- Never include co-author tags unless the project's `/commit` already does.
- If `/check` is missing AND no clear validation exists in `CLAUDE.md` or manifests, report blocked with reason `no project validation defined` rather than committing blind.
- This skill never pushes. If the user wants to ship, they invoke `/ship` themselves.
