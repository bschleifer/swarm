#!/usr/bin/env bash
# PostToolUse hook: detect cross-task file writes and ingest via Swarm API
# Receives tool input JSON on stdin

CROSS_TASK_DIR="$HOME/.swarm/cross-tasks"
SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$FILE_PATH" ] && exit 0

# Resolve relative/symlinked paths to absolute before matching
FILE_PATH=$(realpath "$FILE_PATH" 2>/dev/null || echo "$FILE_PATH")

case "$FILE_PATH" in
    "$CROSS_TASK_DIR"/*.json) ;;
    *) exit 0 ;;
esac

[ ! -f "$FILE_PATH" ] && exit 0

AUTH_HEADER=""
[ -n "$SWARM_API_PASSWORD" ] && AUTH_HEADER="Authorization: Bearer $SWARM_API_PASSWORD"

curl -s -X POST "$SWARM_URL/api/tasks/cross" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
  -d @"$FILE_PATH" > /dev/null 2>&1

rm -f "$FILE_PATH" 2>/dev/null
exit 0
