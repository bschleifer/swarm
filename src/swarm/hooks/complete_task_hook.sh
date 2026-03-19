#!/usr/bin/env bash
# PostToolUse hook: detect task-completion file writes and mark tasks done via Swarm API
# Receives tool input JSON on stdin

COMPLETE_DIR="$HOME/.swarm/complete-task"
SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$FILE_PATH" ] && exit 0

# Resolve relative/symlinked paths to absolute before matching
FILE_PATH=$(realpath "$FILE_PATH" 2>/dev/null || echo "$FILE_PATH")

case "$FILE_PATH" in
    "$COMPLETE_DIR"/*.json) ;;
    *) exit 0 ;;
esac

[ ! -f "$FILE_PATH" ] && exit 0

AUTH_HEADER=""
[ -n "$SWARM_API_PASSWORD" ] && AUTH_HEADER="Authorization: Bearer $SWARM_API_PASSWORD"

TASK_ID=$(jq -r '.task_id // empty' "$FILE_PATH" 2>/dev/null)
RESOLUTION=$(jq -r '.resolution // empty' "$FILE_PATH" 2>/dev/null)

[ -z "$TASK_ID" ] && exit 0

curl -s -X POST "$SWARM_URL/action/task/complete" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "X-Requested-With: XMLHttpRequest" \
  ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
  --data-urlencode "task_id=$TASK_ID" \
  --data-urlencode "resolution=$RESOLUTION" > /dev/null 2>&1

rm -f "$FILE_PATH" 2>/dev/null
exit 0
