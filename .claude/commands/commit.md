---
description: Create a git commit following project conventions
---

Create a git commit with options for local-only or push to main.

## Steps

### Step 1: Format and lint EVERYTHING first
This is mandatory — no exceptions. Run these in order:
```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/
uv run pytest tests/ -q
```
If ruff format touches files you didn't modify, that's fine — they get included in the commit. ALL files must be clean.

### Step 2: Review changes
```bash
git status
git diff --stat
git log -3 --oneline
```

### Step 3: Ask commit scope
Use AskUserQuestion to ask:
- **"Commit locally"** — Just commit to current branch (default for feature work)
- **"Commit and push to main"** — Commit and push to origin/main

### Step 4: Draft commit message
Analyze the changes and write a clear, descriptive commit message. Do NOT ask the user for the message — proceed with a logical summary of what changed.

### Step 5: Create commit
```bash
git add -A
git commit -m "$(cat <<'EOF'
<message>

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

### Step 6: Verify clean tree
After commit, run `git status`. If there are ANY remaining unstaged/untracked changes, stage and amend or create a follow-up commit. The tree MUST be clean before proceeding.

### Step 7: If "Commit and push to main" was selected
```bash
git push origin main
```
After push, run `git status` again to confirm "up to date with 'origin/main'" and "nothing to commit, working tree clean".

### Step 8: Show commit summary
After successful commit, output a text summary (NOT a bash command):

```
═══════════════════════════════════════════════════════════════
COMMIT SUMMARY
═══════════════════════════════════════════════════════════════
Branch:   <branch>
Commit:   <hash> <message>
───────────────────────────────────────────────────────────────
Files changed:
<list files with A/M/D status>
═══════════════════════════════════════════════════════════════
```

Output this directly in your response text — do NOT use echo or bash commands.

## Safety Rules
- Run format + lint + tests before committing (Step 1 is NOT optional)
- Never commit .env, credentials, or secret files
- Never use --amend unless explicitly requested
- Never skip hooks (--no-verify)
- Never force push
- **NEVER declare done with a dirty tree.** If `git status` shows ANY changes after commit, fix it before reporting success.

## After Success
- If committed locally: "Committed locally. Run `/commit` again to push when ready."
- If pushed to main: "Pushed to main."
