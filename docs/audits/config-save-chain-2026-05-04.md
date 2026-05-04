# Config save-chain audit — 2026-05-04

> Phase 1 of the multi-phase fix for the silent-drop bug class
> identified during #328. Maps every `HiveConfig` field through all
> six layers of the save chain to find every silent-drop gap.

**Layers:**

1. **HiveConfig dataclass** (`src/swarm/config/models.py`) — source of truth
2. **`saveSettings()` JS body** (`src/swarm/web/templates/config.html`)
3. **`apply_update` dispatcher** (`src/swarm/server/config_manager.py`)
4. **Per-section `_apply_X` handlers** (`src/swarm/server/config_manager.py`)
5. **`save_config_to_db`** (`src/swarm/db/config_store.py`) — `_SCALAR_KEYS`, `_JSON_KEYS`, normalized tables
6. **`load_config_from_db`** (same file)

> **Bug B note:** the audit pointed at `config_manager.py:156`
> (`self._config.groups = new_config.groups` during YAML hot-reload)
> as a candidate root cause. Verified: `check_file()` has no
> production callers — `dashboard.js:667` only uses the
> `config_file_changed` WS event for client-side UI refresh, it does
> not round-trip back to a server-side reload. Line 156 is therefore
> dead code in this branch but should be hardened defensively. **Bug B
> remains undiagnosed** pending Amanda's controlled before/after
> python query.

## Top-level HiveConfig field coverage

| Field | L1 | L2 | L3 | L4 | L5 | L6 | Status |
|---|---|---|---|---|---|---|---|
| session_name | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| projects_dir | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| provider | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| workers | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| groups | ✓ | ✗ | ✓ | ✗ | ✓ | ✓ | by design — separate `/api/config/groups` endpoints |
| default_group | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| watch_interval | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | **L2,3,4 gap** (medium) |
| drones | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK (sub-fields below) |
| queen | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK (sub-fields below) |
| notifications | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK (post-Bug C fix) |
| coordination | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| jira | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| test | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| terminal | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| resources | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | **CRITICAL** entire section unreachable from UI |
| sandbox | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | **CRITICAL** missing from L2-6 entirely |
| workflows | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| tool_buttons | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| action_buttons | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| task_buttons | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| custom_llms | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| provider_overrides | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| log_level | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| log_file | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | **L2,3,4 gap** (medium) |
| port | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| daemon_url | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | **L2,3,4 gap** (medium) |
| api_password | ✓ | ✗ | ✗ | ✗ | ✓ | ✓ | **L2,3,4 gap** (medium) |
| graph_client_id / tenant_id / client_secret | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| trust_proxy / tunnel_domain / domain | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | OK |

## Nested dataclass coverage

### DroneConfig (25 fields)

| Field | L2 | L3 | L4 | L5 | L6 | Status |
|---|---|---|---|---|---|---|
| enabled | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |
| escalation_threshold..stung_reap_timeout (16 fields) | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| state_thresholds (3 sub-fields) | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| approval_rules | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| allowed_read_paths | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| context_warning_threshold | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |
| context_critical_threshold | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |
| speculation_enabled | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |
| idle_nudge_interval_seconds | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |
| idle_nudge_debounce_seconds | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |

### QueenConfig

All covered except `system_prompt` — intentional (task #253).

### NotifyConfig (post-Bug C)

All 14 fields covered (terminal_bell, desktop, debounce_seconds, desktop_events, terminal_events, templates, webhook.{url,events}, email.{enabled, smtp_host, smtp_port, smtp_user, smtp_password, use_tls, from_address, to_addresses, events}).

### ResourceConfig (11 fields)

**ALL 11 fields missing from L2,3,4** — entire section uneditable from UI:
enabled, poll_interval, elevated_swap_pct, elevated_mem_pct, high_swap_pct, high_mem_pct, critical_swap_pct, critical_mem_pct, suspend_on_high, dstate_scan, dstate_threshold_sec.

### SandboxConfig (3 fields)

**ALL 3 fields missing from L2-6** — added to dataclass but not wired anywhere:
enabled, min_claude_version, settings_overrides.

### WorkerConfig (per-worker)

| Field | L2 | L3 | L4 | L5 | L6 | Status |
|---|---|---|---|---|---|---|
| name / path / description / provider | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| approval_rules | ✓ | ✓ | ✓ | ✓ | ✓ | OK |
| isolation | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |
| identity | ✗ | ✗ | ✗ | ✓ | ✓ | **HIGH** L2,3,4 gap |
| allowed_tools | ✗ | ✗ | ✗ | ✗ | ✗ | **CRITICAL** L2-6 gap |

### Other dataclasses

CoordinationConfig, JiraConfig, TestConfig, TerminalConfig, OversightConfig, StateThresholds, EmailConfig, WebhookConfig — **all OK**.

## Silent-drop bug list (severity-ordered)

### CRITICAL — feature shipped to dataclass but not reachable from any layer

1. `sandbox.*` (3 fields) — `models.py:435-456`. No UI, no apply, no DB persistence.
2. `WorkerConfig.allowed_tools` — `models.py:384`. No UI, no apply, no DB persistence.

### CRITICAL — feature shipped to dataclass + DB but not editable from UI

3. `resources.*` (11 fields) — `models.py:153-169`. DB persists, but operator can't edit.

### HIGH — field editable from CLI/YAML but UI silently drops

4. `drones.enabled` (toggle drones) — `models.py:104`
5. `drones.context_warning_threshold` / `context_critical_threshold` — context awareness
6. `drones.speculation_enabled`
7. `drones.idle_nudge_interval_seconds` / `idle_nudge_debounce_seconds`
8. `WorkerConfig.isolation` (worktree mode)
9. `WorkerConfig.identity` (per-worker CLAUDE.md path)

### MEDIUM — rarely-edited top-level scalars not surfaced in UI

10. `watch_interval`, `log_file`, `daemon_url`, `api_password`

### LOW — deprecated/by-design exclusions

- `queen.system_prompt` (deprecated UI)
- `terminal.replay_max_bytes` (deprecated)
- `groups` not in saveSettings — uses dedicated `/api/config/groups` endpoints by design

## Bug B candidate: groups overwrite during YAML hot-reload

**Location:** `src/swarm/server/config_manager.py:156` — `self._config.groups = new_config.groups`

**Risk:** if hot-reload ever fires with a YAML that has no/empty groups section, in-memory groups get overwritten with `[]`, then the next save persists the empty list to DB.

**Status:** **dead code in current build** — `check_file()` has no production caller. Dashboard's `config_file_changed` WS event only triggers client-side `refreshWorkers()` / `refreshStatus()`, not server-side reload. Bug B remains undiagnosed.

**Recommendation:** harden line 156 defensively (preserve groups when YAML lacks them, mirror the existing `approval_rules` preservation pattern at lines 152-154) so we don't ship a footgun if anyone wires up `check_file()` later.

## Recommendations for Phases 2–5

### Phase 2 — fail-loud guard

Add post-dispatch sweep in `apply_update`: walk body keys, log WARNING (or 400) for any key not consumed. Catches future schema drift the moment dashboard or server adds a field without the other.

This won't catch the existing L2,3,4 gaps because the dashboard doesn't send those fields either. To catch them we need Phase 5 (dashboard reconciliation) too.

### Phase 3 — generic dataclass dispatch

Replace per-section `_apply_X` handlers with a single dataclass-aware applier that introspects `dataclass.__dataclass_fields__`. New field: add to dataclass once, the rest follows. Eliminates bug class.

### Phase 4 — round-trip test

One test that constructs a `HiveConfig` with a non-default value for every leaf field, walks it through `apply_update → save → reload from DB`, asserts equality. Locks the contract for every field.

### Phase 5 — dashboard reconciliation

After autosave, refetch `/api/config` and re-render. Surface divergence as a toast naming the lost fields. Catches gaps that exist before our backend fixes land.

## Summary stats

- 33 top-level HiveConfig fields
- ~80 nested dataclass leaf fields
- **22 affected fields** with at least one layer gap
  - 3 critical L2-6 gaps (sandbox.*, allowed_tools)
  - 11 critical L2-4 gaps (resources.*)
  - 8 high L2-4 gaps (drone advanced + worker isolation/identity)
  - 4 medium L2-4 gaps (top-level scalars)
- Bug B unresolved — undiagnosed; dead-code candidate in `config_manager.py:156`
