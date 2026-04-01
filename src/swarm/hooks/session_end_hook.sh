#!/usr/bin/env bash
# SessionEnd hook: notify Swarm daemon that a Claude Code session has ended.
# This enables immediate STUNG detection without /proc polling.

SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)

# Extract session metadata if available
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)

AUTH_HEADER=""
[ -n "$SWARM_API_PASSWORD" ] && AUTH_HEADER="Authorization: Bearer $SWARM_API_PASSWORD"

curl -s --max-time 3 -X POST "$SWARM_URL/api/hooks/session-end" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
  -d "$INPUT" > /dev/null 2>&1

exit 0
