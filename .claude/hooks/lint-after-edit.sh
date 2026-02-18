#!/bin/bash
# PostToolUse hook: run ruff check on edited Python files

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only check Python files
if [[ ! "$FILE_PATH" =~ \.py$ ]]; then
  exit 0
fi

# Skip test config files and non-source files
if [[ "$FILE_PATH" =~ (conftest|setup|pyproject) ]]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR" || exit 0

# Run ruff check (fast, ~0.1s per file)
LINT_OUTPUT=$(uv run ruff check "$FILE_PATH" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
  ERRORS=$(echo "$LINT_OUTPUT" | grep -c "Found" || echo "unknown")
  echo "$LINT_OUTPUT" >&2
  exit 2
fi

exit 0
