---
description: Pull latest from origin/main and merge with current work
---

Sync your current branch with the latest changes from origin/main.

## Workflow

### Step 1: Stash any uncommitted changes (if needed)
```bash
git status --porcelain
```
If there are uncommitted changes:
```bash
git stash push -m "Auto-stash before get-latest"
```

### Step 2: Fetch and merge
```bash
git fetch origin main
git merge origin/main --ff-only || git merge origin/main -m "Merge latest from origin/main"
```

### Step 3: Restore stashed changes (if any)
```bash
git stash pop
```

### Step 4: Report status
```bash
git log --oneline -5
```

## Handling Merge Conflicts
If merge conflicts occur:
1. List conflicting files: `git diff --name-only --diff-filter=U`
2. Report to user with the list of conflicts
3. Do NOT automatically resolve conflicts - let the user handle them
