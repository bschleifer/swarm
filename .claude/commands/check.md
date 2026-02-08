---
description: Run pre-commit validation (ruff + pytest) - MANDATORY before any commit
---

Run the pre-commit validation suite. All checks must pass with ZERO warnings.

## Checks Performed
1. Ruff format — apply formatting (not just check)
2. Ruff lint (`uv run ruff check src/ tests/`)
3. Pytest (`uv run pytest tests/ -x -q`)

## Execution

First, FORMAT everything (don't just check — fix it):

```bash
uv run ruff format src/ tests/
```

Then lint:

```bash
uv run ruff check src/ tests/
```

Then test:

```bash
uv run pytest tests/ -x -q
```

Finally, verify tree state:

```bash
git status
```

## On Success
Report: "All checks passed. Ready for /commit."

If `git status` shows changes from formatting, mention them — they must be included in the next commit.

## On Failure
1. Identify which check(s) failed
2. Show the specific errors
3. Do NOT proceed with /commit until ALL issues are fixed
4. Try auto-fixes first:
   - Lint: `uv run ruff check --fix src/ tests/`
   - Format: `uv run ruff format src/ tests/`
5. Re-run to verify

## Common Fixes
- **Lint errors**: Run `uv run ruff check --fix src/ tests/` for auto-fixable issues
- **Format errors**: Run `uv run ruff format src/ tests/` to auto-format
- **Test failures**: Read the failing test, understand what it expects
- **Import errors**: Check that `uv sync` has been run
