# Swarm WebSocket Protocol

## Endpoints

| Path | Purpose |
|------|---------|
| `/ws` | Main event channel (state updates, proposals, notifications) |
| `/ws/terminal` | Terminal I/O (PTY read/write for a specific worker) |

## Authentication

Both endpoints require first-message auth within 5 seconds of connection:

```json
{"type": "auth", "token": "your-api-password"}
```

**Failure responses:**
- Timeout (5s): Close code `4001`, message "Auth timeout"
- Invalid JSON: Close code `4001`, message "Invalid auth message"
- Wrong token: Close code `4001`, message "Unauthorized"

**Lockout:** 5 failed auth attempts from the same IP triggers a 5-minute lockout.

**Per-IP limit:** Maximum 10 concurrent WebSocket connections per IP.

**Deprecated:** `?token=` query parameter still accepted but logs a warning.

## Main WebSocket (`/ws`)

### Init Message (server → client)

Sent immediately after successful auth:

```json
{
  "type": "init",
  "workers": [
    {"name": "api", "state": "BUZZING", "duration": 12.5}
  ],
  "drones_enabled": true,
  "proposals": [...],
  "proposal_count": 2,
  "queen_queue": {"running": 0, "pending": 0},
  "test_mode": false
}
```

### Event Types (server → client)

| Event Type | Trigger | Key Fields |
|------------|---------|------------|
| `workers_changed` | Worker state change | (triggers client refresh) |
| `tasks_changed` | Task created/updated/deleted | (triggers client refresh) |
| `pipelines_changed` | Pipeline state change | (triggers client refresh) |
| `proposal_created` | Queen proposes an action | `proposal` object |
| `proposal_resolved` | Proposal approved/rejected | `proposal_id`, `status` |
| `resources` | Resource monitor tick (~30s) | `mem_percent`, `swap_percent`, `load_1m`, `pressure_level` |
| `dstate_alert` | D-state process detected | `pids` map |
| `ownership_overlap` | File ownership conflict | `overlaps` array |
| `tunnel_started` | Tunnel goes up | `url` |
| `tunnel_stopped` | Tunnel goes down | (empty) |
| `tunnel_error` | Tunnel failure | `error` message |
| `config_updated` | Config changed via API | (triggers client refresh) |
| `drone_action` | Drone makes a decision | `action`, `worker`, `detail` |
| `notification` | Push notification event | `event`, `title`, `message`, `severity` |

### Client Commands (client → server)

| Command | Purpose |
|---------|---------|
| `{"command": "focus", "worker": "name"}` | Tell server which worker the client is viewing |

### Heartbeat

Server sends heartbeat data every ~20 seconds via `workers_changed` events. No explicit ping/pong frames — aiohttp handles WebSocket-level pings automatically.

## Terminal WebSocket (`/ws/terminal`)

### Auth + Worker Selection

```json
{"type": "auth", "token": "...", "worker": "api"}
```

Or connect with query param: `/ws/terminal?worker=api`

### Data Flow

- **Server → Client:** Raw terminal output (binary frames, UTF-8 text)
- **Client → Server:** Keystrokes (text frames)
- **Resize:** `{"type": "resize", "cols": 120, "rows": 40}`

### Connection Lifecycle

1. Client connects and authenticates
2. Server replays recent terminal buffer (~last 50KB)
3. Bidirectional streaming begins
4. On disconnect, server keeps the PTY running (reconnect resumes)

## Error Codes

| Code | Meaning |
|------|---------|
| 4001 | Authentication failed |
| 4002 | Worker not found |
| 4003 | Rate limited (too many connections) |
| 1000 | Normal close |
