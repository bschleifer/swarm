---
description: Create a git commit following project conventions
---

Create a git commit with options for local-only or push to main.

## Steps

### Step 1: Verify /check was run
If unsure whether checks pass, run `/check` first.

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

### Step 6: If "Commit and push to main" was selected
```bash
git push origin main
```

### Step 7: Show commit summary
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
- Run /check before committing
- Never commit .env, credentials, or secret files
- Never use --amend unless explicitly requested
- Never skip hooks (--no-verify)
- Never force push

## After Success
- If committed locally: "Committed locally. Run `/commit` again to push when ready."
- If pushed to main: "Pushed to main."
