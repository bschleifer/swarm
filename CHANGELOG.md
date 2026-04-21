# Changelog

Swarm uses calendar versioning (`YYYY.M.D.patch`) — see `pyproject.toml` for the current version. Notable changes since the initial v1.0.0 release are grouped below.

## Unreleased

### Features
- **Autonomous worker momentum (task #225).** Workers no longer park on newly assigned tasks waiting to be polled — Swarm now *pushes* work in three coordinated ways:
  - **Phase 1: task-push dispatch on assignment.** `swarm_create_task(target_worker=X)` routes through `daemon.assign_and_start_task()` by default, which injects the task description straight into X's PTY within one poll cycle. Previously the handler only called `assign_task`, leaving the task queued in ASSIGNED status with nothing dispatching it — that's the root of the recurring "5 workers with hours-old in_progress tasks" operator-pain pattern. New `start: bool` argument on the MCP tool (default `true`) preserves queue-only behavior for Queen/operator staging flows (`start=false`). Self-targeted tasks (caller == target) never dispatch — no interleaving with the caller's own turn.
  - **Phase 2: idle-watcher drone (`drones/idle_watcher.py`).** Periodic sweep (`DroneConfig.idle_nudge_interval_seconds`, default 180 s) nudges RESTING / SLEEPING workers that have an ASSIGNED / IN_PROGRESS task but aren't moving on it. Nudge message points the worker at `swarm_task_status filter=mine` + `swarm_check_messages` so it can self-diagnose rather than treating the nudge as a fresh prompt. Per-(worker, task) debounce (default 900 s) prevents spam; new `AUTO_NUDGE` action in `DroneAction`/`SystemAction` makes every auto-prompt auditable in the buzz log. Rate-limited workers are skipped so we don't stack work behind a dead Claude quota.
  - **Phase 3: post-ship self-loop.** `daemon.complete_task()` now fires `start_task()` for the next ASSIGNED task belonging to the same worker (lowest task number first) as soon as the current one ships. IN_PROGRESS follow-ups are skipped — they're already running somewhere else. Empty queues get no follow-up, per spec ("skip if the worker has nothing else assigned, avoid pointless loops").
  - 19 new tests in `tests/test_idle_watcher.py`, `tests/test_mcp_tools.py::TestCreateTaskAutoDispatch`, and `tests/test_daemon.py` (post-ship auto-start). Full suite: 3828 tests pass. CLAUDE.md gained a new "Autonomous task momentum" section documenting the push semantics for future operators.

### Changes

### Fixes
- **Post-restart terminal reload race — output dropped during discovery window.** When the daemon `os.execv`s (the dashboard Reload button's happy path), `ProcessPool.connect()` starts the holder read loop immediately — but the worker map (`_workers`) is still empty and only gets populated one worker at a time by `discover()`, which does a separate snapshot roundtrip per worker. For the ~1–3 seconds that took, any live PTY output the holder broadcast for a not-yet-discovered worker was silently dropped in `_dispatch_message`. That's the race behind the long-standing "type in the terminal, nothing shows, a second Reload fixes it" bug: the worker's local ring buffer was missing a chunk, which sometimes truncated ANSI escape sequences and left the xterm in a glitched state. The fix buffers unknown-worker output into `_pending_output` and relies on the read loop's serial ordering: any chunks already buffered when the snapshot response resolves are pre-snapshot (already inside the snapshot bytes, dropped to avoid duplication); anything that arrives after resolution routes directly to the now-registered `WorkerProcess.feed_output`. Two new tests in `tests/test_pool.py` lock both paths in. Diagnostic `[term-trace]` logging added in the same session stays put until the reload flow has been stable through several restarts.
- **Operator bypass for the PreToolUse approval hook.** `src/swarm/hooks/approval_hook.sh` now honors a `SWARM_OPERATOR=1` escape hatch alongside the existing `SWARM_MANAGED=1` guard — the PTY holder exports `SWARM_MANAGED=1` for *every* worker it spawns, including sessions the operator is driving interactively, so the old "operator's own session is never gated" invariant was unreachable without a second marker. Operators who want a worker session to bypass drone approval rules (e.g. running `/ship` from an attached worker) now set `export SWARM_OPERATOR=1` in that session and the hook exits early before contacting the daemon. The comment at the top of the script was rewritten to describe this boundary accurately. Pinned by three new tests in `tests/test_approval_hook_script.py` that exercise the shell script against a counting HTTP stub (task #211).

## [2026.4.20] - 2026-04-20

### Features

### Changes

### Fixes

## [2026.4.19] - 2026-04-19

### Features
- **MCP `tools/list_changed` push on SSE connect.** The MCP server now advertises the `tools.listChanged` capability on initialize and, the moment a client opens the streamable SSE stream (GET `/mcp`) or the legacy SSE stream (GET `/mcp/sse`), pushes a `notifications/tools/list_changed` JSON-RPC message. Conformant MCP clients react by re-calling `tools/list`, so schemas cached from a pre-reload daemon no longer linger on the client side. Closes the gap exposed by task #169 — the fix had landed server-side but worker/host sessions kept the stale tool schema in their local cache because nothing told them to refresh. Legacy SSE's required first event (the `endpoint` URL) is preserved; the refresh notification is the second event. Four new integration tests in `tests/test_mcp_server.py` pin the behaviour.

### Changes

### Fixes

## [2026.4.18.3] - 2026-04-18

### Features
- **MCP tool schema-drift indicator.** `src/swarm/mcp/tools.py` hashes itself at import time; `tools_source_drift()` compares the frozen hash against the current file contents. The dev-mode dashboard footer polls `/api/health` every 30s (new `mcp_schema_drift` field) and highlights the Reload button in honey with "Reload needed (MCP tools edited)" status when the source has changed since daemon start. Standalone `GET /api/mcp/schema-drift` endpoint returns the full `{drift, source_path, startup_hash, current_hash}` payload for external tooling. Surfaces the exact scenario that hid task #169's fix in the running daemon until someone noticed the call still used the legacy code path.
- **Reload button on the config page header.** The dashboard footer Reload button is hidden on mobile, so the same dev-reload flow (POST `/api/server/restart`, poll `/api/health` until the daemon comes back, refresh the page) is now reachable from the config page header. Only rendered when `is_dev` is True.

### Changes

### Fixes

## [2026.4.18.2] - 2026-04-18

### Features

### Changes
- **Queen banners de-dup per worker, not per text.** The dashboard's queen/escalation banners now key dedup off a `data-worker` attribute instead of string-comparing `textContent`, so two banners for the same worker with different copy don't pile up. Selecting a worker in the sidebar now also removes any lingering banners tied to that worker — the operator is addressing it directly, the banner no longer adds signal.

### Fixes
- **`swarm_complete_task` silently closed the wrong task when a worker had multiple in_progress assignments (task #169).** The handler walked `task_board.all_tasks` and closed the first match for the calling worker, arbitrarily picking one task and attaching the caller's resolution to it. The MCP tool now takes an optional `number` parameter: singular active task + no `number` keeps the legacy behaviour, multiple active tasks + no `number` errors with the candidate list instead of guessing, and an explicit `number` validates ownership + status before closing. Seven regression tests pin the new contract.
- **Swarm's own MCP tools (`mcp__swarm__*`) could stall behind a PreToolUse permission prompt.** The hook handler (`routes/hooks.py`) now short-circuits to `approve` for any tool name starting with `mcp__swarm__` — these are the daemon's own coordination primitives (`swarm_check_messages`, `swarm_complete_task`, `swarm_task_status`, …) and gating them behind operator approval could leave a worker waiting indefinitely on something that's definitionally safe. Non-swarm MCP tools (e.g. `mcp__stripe__*`) still flow through the normal rules engine.

## [2026.4.18] - 2026-04-18

### Features

### Changes

### Fixes

## [2026.4.17.2] - 2026-04-17

### Features
- **Dashboard "Awaiting your input" pill on worker tiles.** When a worker sits in WAITING state past a 15-second grace window, the tile now shows a pulsing amber pill to make operator-action-required cases visually distinct from a plain WAITING badge. Drives off a new `Worker.needs_operator_input` property exposed via the workers API. Fixes the common confusion where a worker presenting an `AskUserQuestion` prompt looked indistinguishable from a stalled/silent worker.

### Fixes
- **Cross-project task attribution on MCP `swarm_create_task`.** When a worker called `swarm_create_task` with `target_worker=X`, the resulting task row landed in the DB with `source_worker=""` — the calling worker's identity was lost. The handler now calls `edit_task` to record `source_worker` (the calling worker) alongside `target_worker` before assigning, so `is_cross_project` lineage is preserved end-to-end. Self-targeted tasks skip the edit to avoid spurious cross-project flags.

## [2026.4.17] - 2026-04-17

### Features
- **`swarm_batch` MCP tool** — ninth coordination tool; runs multiple `swarm_*` ops sequentially in one round-trip so a worker no longer pays N round-trips for N related calls. Nested `swarm_batch` is rejected to prevent runaway recursion. Each op is still buzz-logged individually.
- **Richer MCP tool descriptions** — every `swarm_*` tool now carries a ≥150-char description with trigger hints ("when to call"), enum semantics (e.g. `finding` vs `warning` vs `dependency` vs `status`), and concrete `examples` in the input schema.
- **`swarm analyze-tools` CLI** — aggregates MCP tool usage from the buzz log (`mcp:*` entries) into per-tool stats: calls, errors, active workers, and up to five distinct error snippets per tool. Supports `--since=7d`, `--json` output, and `--db PATH` for offline DB analysis.
- **Approval-rate gauge** — `SystemLog.approval_rate(since=...)` returns `{approvals, escalations, rate}` from recent decisions; new `GET /api/drones/approval-rate?hours=N` endpoint; dashboard header badge shows the percentage over the last 24h.
- **`DroneDecision.confidence`** — optional float field so future LLM-classifier rules can slot in next to the existing rule-based decisions without a schema change.
- **Compact event telemetry** — every `/compact` logs a `SystemAction.COMPACT` entry under new `LogCategory.COMPACT` with `{tokens_before, tokens_after, ratio, trigger}` metadata. Makes compaction effectiveness measurable per worker and per run.
- **Cron-format pipeline schedules** — pipeline steps now accept full 5-field cron expressions (e.g. `"30 14 * * 1-5"` for weekdays at 14:30). Legacy `HH:MM`, `*:MM`, and `HH:*` still work and are translated to cron internally. Adds `croniter` as a dependency.
- **Skills registry** — SQLite-backed skills table (schema v5 migration, idempotent `CREATE TABLE IF NOT EXISTS`). `SkillsStore` CRUD + usage counters; `attach_skills_store()` seeds built-in defaults (`/fix-and-ship`, `/feature`, `/verify`) on first boot. New `GET /api/skills` endpoint. `get_skill_command()` consults the registry before falling back to the in-memory map and increments `usage_count` on each lookup.
- **`claude_code_security` service handler** — new pipeline AUTOMATED step that runs `claude code security scan --json`, parses the findings array, maps severity to Swarm task priority (`critical→urgent`, `high→high`, `medium→normal`, `low/info→low`), and deduplicates against a persistent state file fingerprinted by `sha256(rule_id\x00path\x00line)`. Supports `severity_filter`, configurable command, and custom dedup state path.
- **Test harness infra pinning** — every `swarm test` run captures an `InfraSnapshot` (model, provider, worker_count, port, claude_home, swarm_version, python_version, platform, env_hash, env_keys) and writes it as the first line of `test-run-{id}.jsonl`. The Markdown report gains an "Infrastructure Snapshot" section above the summary. New `swarm test --pin-model=<id>` flag records the model identifier explicitly, and `compute_env_hash` fingerprints tracked env vars (CLAUDE_MODEL, SWARM_PROVIDER, etc.) via SHA-256 so infra drift is debuggable without leaking secrets.
- **Opt-in Claude Code sandbox** — new `sandbox:` config block on `HiveConfig` (`{enabled, min_claude_version, settings_overrides}`). When enabled, `hooks.install.install()` calls `claude --version`, verifies the installed CC version meets `min_claude_version`, and merges `settings_overrides` into `~/.claude/settings.json["sandbox"]`. Unsupported or missing versions silently stay on the legacy approval flow. Disabled by default; no behaviour change for existing installs.
- **In-app feedback** — report bugs, feature requests, and questions directly from the dashboard footer. Submissions go through the GitHub CLI (`gh`) to bypass URL length limits, with a preview-and-edit step before the issue is filed. Sensitive paths and config values are auto-redacted.
- **Resource monitoring** — memory, swap, and load tracked on a 30s tick; workers auto-suspend on HIGH pressure and the operator is paged on CRITICAL. D-state (wedged process) scanning is optional.
- **Jira integration** — two-way sync with Jira Cloud over OAuth 2.0 (3LO). Import issues as tasks, push status and completion comments back, create Jira issues from the task board.
- **Email integration** — Microsoft Graph (Outlook) integration: drop `.eml`/`.msg` onto the task board, fetch emails from the dashboard, and draft a reply in the Drafts folder when a task completes (never auto-sent).
- **MCP server** — HTTP-based MCP server at `/mcp` (Streamable HTTP + legacy SSE). Workers get 9 coordination tools: `swarm_check_messages`, `swarm_send_message`, `swarm_task_status`, `swarm_create_task`, `swarm_complete_task`, `swarm_report_progress`, `swarm_claim_file`, `swarm_get_learnings`, `swarm_batch`.
- **Inter-worker messages** — typed messages (finding, warning, dependency, status, operator) delivered via MCP; dedup + rate-limit per `(sender, recipient, type)` pair.
- **Pipelines** — multi-step workflows combining AGENT, AUTOMATED, and HUMAN steps with per-step dependencies, templates, and start/pause/resume lifecycle. State persisted in SQLite.
- **Queen oversight** — proactive monitoring: prolonged-buzzing detection and task-drift analysis; interventions classified by severity (minor note, pause+redirect, escalate to operator).
- **File ownership & coordination** — single-branch mode (default) with Queen-managed file ownership map; warning or hard-block on overlap; worktree escape hatch when scopes are unavoidable.
- **Auto-pull sync** — workers auto-pull when another worker commits on the shared branch.
- **Multi-provider support** — Claude Code (production), Gemini CLI and Codex CLI (experimental), plus custom providers via `custom_llms` and per-provider overrides.
- **Cloudflare Tunnel** — one-click remote HTTPS access from the dashboard toolbar; optional named-domain configuration via `tunnel_domain`.
- **Dashboard push notifications** — browser push + desktop notifications + terminal bell; persistent Buzz Log history.
- **Interactive terminal attach** — full xterm.js PTY bridge over WebSocket, up to 20 concurrent sessions.
- **PWA** — installable app with service-worker offline shell and badge API for pending proposals.
- **Config editor in the dashboard** — tabbed UI for workers, groups, drones, Queen, workflows, and integrations; changes apply immediately.
- **Drone log & tuning analytics** — per-rule hit stats and AI-suggested approval rule patterns.
- **Speculation (experimental)** — preparatory read-only work on a queued task while a worker is RESTING.
- **Swarm CLI: `swarm db`** — `stats`, `export`, `prune`, `backup`, `check` for inspecting and maintaining the unified SQLite store.
- **Swarm CLI: `swarm test`** — supervised end-to-end orchestration test against a dedicated port with an AI-generated report.
- **Claude Code hook integration** — PreToolUse (drone-based approval), SessionEnd (immediate STUNG detection), and event hooks (SubagentStart/Stop, PreCompact/PostCompact) installed automatically by `swarm init`.

### Changes
- **Unified SQLite storage** — tasks, task history, proposals, messages, pipelines, buzz log, queen sessions, secrets, and config itself all live in `~/.swarm/swarm.db` (WAL mode). The legacy YAML is treated as a seed/import format; the database is the runtime source of truth after first run.
- **Jira auth is OAuth-only** — token auth was removed in favor of Atlassian OAuth 2.0 (3LO).
- **Config mutations are immediate** — dashboard edits write straight to the DB and hot-apply in the same request.
- **Calendar versioning** — version now tracks release date (`YYYY.M.D.patch`) rather than semver; the v1.0.0 section below is preserved for history.

### Fixes
- Numerous fixes to feedback submission (live `HiveConfig` serialization, `gh` CLI fallback for 8 KB URL limits, preview/edit gate before submission).
- See `git log` for the full per-commit history.

---

## v1.0.0

Initial release of Swarm — a hive-mind orchestrator for Claude Code agents.

### Features
- **Web Dashboard** — Browser-based dashboard with real-time WebSocket updates, inline terminal, and full task management
- **Worker Management** — Launch, kill, revive, and monitor Claude Code agents running in managed PTYs
- **Task Board** — Create, assign, complete, and track tasks with priority, tags, dependencies, and file attachments
- **Drones** — Background automation: auto-continue idle workers, auto-approve prompts, escalate stuck agents
- **Queen** — Headless Claude conductor for hive-wide coordination and per-worker analysis
- **Groups** — Organize workers into named groups for targeted broadcasts and management
- **Config** — YAML-based configuration with live-reload and web-based config editor
- **Notifications** — Browser notifications, terminal bell, and persistent Buzz Log
- **Task History** — Audit log tracking full task lifecycle events
- **Themed UI** — Warm beehive color palette, responsive layout, keyboard shortcuts
