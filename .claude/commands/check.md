---
description: Run pre-commit validation (ruff + pytest) - MANDATORY before any commit
---

Run the pre-commit validation suite. All checks must pass with ZERO warnings.

## Checks Performed
1. Ruff lint (`uvx ruff check .`)
2. Ruff format (`uvx ruff format --check .`)
3. Pytest (`uv run pytest tests/ -x -q`)

## Execution

Run all three checks sequentially:

```bash
uvx ruff check .
```

```bash
uvx ruff format --check .
```

```bash
uv run pytest tests/ -x -q
```

## On Success
Report: "All checks passed. Ready for /commit."

## On Failure
1. Identify which check(s) failed
2. Show the specific errors
3. Do NOT proceed with /commit until ALL issues are fixed
4. Try auto-fixes first:
   - Lint: `uvx ruff check --fix .`
   - Format: `uvx ruff format .`
5. Re-run to verify

## Common Fixes
- **Lint errors**: Run `uvx ruff check --fix .` for auto-fixable issues
- **Format errors**: Run `uvx ruff format .` to auto-format
- **Test failures**: Read the failing test, understand what it expects
- **Import errors**: Check that `uv sync` has been run
