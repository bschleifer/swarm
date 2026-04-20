#!/usr/bin/env bash
# PreToolUse hook: query Swarm daemon for approval decision.
# Claude Code sends tool_name + tool_input on stdin as JSON.
# We forward it to the daemon and return the approval decision as JSON on stdout.
# If the daemon is unreachable or returns an error, we pass through (no decision).
#
# Active when SWARM_MANAGED=1 is set (the PTY holder exports it for every
# worker session it spawns — both autonomous workers and operator-attached
# ones). An operator who is driving a worker interactively can opt out by
# also exporting SWARM_OPERATOR=1 in that session; the hook then exits
# early and no drone rule gates their tool calls. See
# docs/hooks-operator-bypass.md for the full boundary.

# Skip if not a Swarm-managed worker
[ "$SWARM_MANAGED" != "1" ] && exit 0

# Operator escape hatch: interactive operator sessions opt out of drone rules.
[ "$SWARM_OPERATOR" = "1" ] && exit 0

SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)

# Extract tool name for quick bail-out on safe tools handled by Claude Code itself
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
[ -z "$TOOL_NAME" ] && exit 0

AUTH_HEADER=""
[ -n "$SWARM_API_PASSWORD" ] && AUTH_HEADER="Authorization: Bearer $SWARM_API_PASSWORD"

# Query the daemon for an approval decision
RESPONSE=$(curl -s --max-time 4 -X POST "$SWARM_URL/api/hooks/approval" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
  -d "$INPUT" 2>/dev/null)

# If curl failed or returned empty, pass through (let Claude Code handle it normally)
[ -z "$RESPONSE" ] && exit 0

# Extract the decision from the daemon response
DECISION=$(echo "$RESPONSE" | jq -r '.decision // empty' 2>/dev/null)

case "$DECISION" in
  approve)
    echo '{"decision":"approve"}'
    ;;
  block)
    REASON=$(echo "$RESPONSE" | jq -r '.reason // "Blocked by Swarm drone rules"' 2>/dev/null)
    echo "{\"decision\":\"block\",\"reason\":$(echo "$REASON" | jq -Rs .)}"
    ;;
  *)
    # No decision or unknown — pass through
    exit 0
    ;;
esac

exit 0
