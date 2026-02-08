#!/bin/bash
# Pre-commit validation hook for Claude Code
# Runs ruff lint and format checks before git commit

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Only validate git commit commands
if ! echo "$COMMAND" | grep -qE '^git commit'; then
  exit 0
fi

# Run validation checks
ERRORS=""
EXIT_CODE=0

# Ruff lint
LINT_OUT=$(uvx ruff check . 2>&1)
if [ $? -ne 0 ]; then
  ERRORS="${ERRORS}Ruff lint errors:\n${LINT_OUT}\n\n"
  EXIT_CODE=1
fi

# Ruff format
FMT_OUT=$(uvx ruff format --check . 2>&1)
if [ $? -ne 0 ]; then
  ERRORS="${ERRORS}Ruff format errors:\n${FMT_OUT}\n\n"
  EXIT_CODE=1
fi

if [ $EXIT_CODE -ne 0 ]; then
  REASON="Pre-commit checks failed. Fix these issues before committing:\n${ERRORS}"
  jq -n --arg reason "$REASON" '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": $reason
    }
  }'
  exit 0
fi

# All checks passed
exit 0
