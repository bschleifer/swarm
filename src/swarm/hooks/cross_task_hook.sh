#!/usr/bin/env bash
# PostToolUse hook: detect cross-task file writes and ingest via Swarm API
# Receives tool input JSON on stdin

CROSS_TASK_DIR="$HOME/.swarm/cross-tasks"
SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$FILE_PATH" ] && exit 0

case "$FILE_PATH" in
    "$CROSS_TASK_DIR"/*.json) ;;
    *) exit 0 ;;
esac

[ ! -f "$FILE_PATH" ] && exit 0

curl -s -X POST "$SWARM_URL/api/tasks/cross" \
  -H "Content-Type: application/json" \
  -d @"$FILE_PATH" > /dev/null 2>&1

rm -f "$FILE_PATH" 2>/dev/null
exit 0
