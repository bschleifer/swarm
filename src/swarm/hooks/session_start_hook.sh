#!/usr/bin/env bash
# SessionStart hook: ask the Swarm daemon for a per-worker bootstrap
# (assigned task + unread inter-worker messages) and inject it into Claude's
# context via additionalContext.
#
# Claude Code passes the SessionStart hook input on stdin as JSON:
#   {"session_id": "...", "cwd": "...", "hook_event_name": "SessionStart",
#    "source": "startup"|"resume"|"clear"|"compact", ...}
#
# We forward the body to /api/hooks/session-start and echo the daemon's
# response on stdout. The daemon returns a hookSpecificOutput payload that
# Claude Code reads directly. Resume sessions are skipped server-side.
#
# Only active for Swarm-managed workers (SWARM_MANAGED=1 set by holder).
# Fail-open: if the daemon is unreachable or errors out, we exit 0 with no
# output so the worker still starts cleanly.

# Skip if not a Swarm-managed worker
[ "$SWARM_MANAGED" != "1" ] && exit 0

SWARM_URL="${SWARM_URL:-http://localhost:9090}"

INPUT=$(cat)
[ -z "$INPUT" ] && exit 0

AUTH_HEADER=""
[ -n "$SWARM_API_PASSWORD" ] && AUTH_HEADER="Authorization: Bearer $SWARM_API_PASSWORD"

# Query the daemon for the bootstrap payload
RESPONSE=$(curl -s --max-time 3 -X POST "$SWARM_URL/api/hooks/session-start" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  ${AUTH_HEADER:+-H "$AUTH_HEADER"} \
  -d "$INPUT" 2>/dev/null)

# If curl failed or returned empty, fail open — the worker still starts
[ -z "$RESPONSE" ] && exit 0

# Echo the daemon's JSON response so Claude Code reads it as the hook output
echo "$RESPONSE"
exit 0
