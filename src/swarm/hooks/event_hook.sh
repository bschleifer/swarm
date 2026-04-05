#!/usr/bin/env bash
# Generic hook: forward Claude Code lifecycle events to Swarm daemon.
# Used for SubagentStart, SubagentStop, PreCompact, PostCompact, TeammateIdle.
# Claude Code's hook_event_name field in the input identifies which event fired.
# Only active for Swarm-managed workers.

[ "$SWARM_MANAGED" != "1" ] && exit 0

SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)

AUTH_HEADER=""
[ -n "$SWARM_API_PASSWORD" ] && AUTH_HEADER="Authorization: Bearer $SWARM_API_PASSWORD"

curl -s --max-time 3 -X POST "$SWARM_URL/api/hooks/event" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
  -d "$INPUT" > /dev/null 2>&1

exit 0
