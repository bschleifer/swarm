---
title: "Context-usage data source — Phase 0 audit (task #285)"
status: shipped
shipped_date: 2026-04-26
related_specs:
  - ../specs/headless-queen-architecture.md
---

# Context-Usage Data Source — Phase 0 Audit

**Date:** 2026-04-26
**Plan reference:** `~/.claude/plans/sequential-churning-meerkat.md` Item 3
**Question:** Is `worker.usage` reliable enough to drive a context-pressure manager that auto-injects `/compact`, or do we need a different signal?

> **Status: SHIPPED (2026-04-26 as task #285 Phase 1).** The action layer landed in `src/swarm/drones/context_pressure.py`; this doc remains as the design reference. See the CHANGELOG entry under the v2026.4 series and `src/swarm/drones/context_pressure.py` for as-built behaviour.

---

## TL;DR

**Use the existing signal directly.** No PTY parsing, no JSONL reads, no hybrid fallback. The infrastructure is already in place — Phase 1 just needs to add the missing **action** layer that takes the already-emitted pressure notifications and turns them into `/compact` injections. Estimated Phase 1 scope: a single new drone (~150 LOC) plus dashboard chip.

---

## What's already in place

### 1. Authoritative data source: session JSONL

`src/swarm/worker/usage.py::get_worker_usage(worker_path, ...)` reads the canonical Claude Code session JSONL at `~/.claude/projects/<encoded-worker-path>/*.jsonl`.

- **No PTY parsing dependency.** Operator's status-line config is irrelevant. Works for every worker regardless of provider tuning.
- **Path encoding hardened.** `project_dir(worker_path)` replaces both `/` and `.` with `-` (the dotted-path fix that landed for the Queen workdir).
- **Picks the most-recent JSONL** via `find_active_session(proj_dir, since)` — `since` is now a no-op param (commented as such), avoiding the daemon-restart issue where mtime gating blanked usage.
- **Reads `usage.input_tokens` per assistant turn** and tracks `last_turn_input_tokens` separately. The docstring at `usage.py:217` explicitly notes this is the right proxy because **cumulative totals grow monotonically and don't reflect compaction**.

### 2. Refresh cadence: 15s

`SwarmDaemon._usage_refresh_loop` (daemon.py:972) runs every `_USAGE_REFRESH_INTERVAL` seconds (15s by default), refreshes every worker's usage in parallel under a semaphore, and updates two derived fields:
- `worker.context_pct` — `estimate_context_usage(usage, provider_name)` returns 0.0–1.0
- `worker.cache_ratio` — read efficiency

15s is fast enough for context pressure (workers don't burn through 20% of context in <15s during normal operation; even during heavy tool use they typically take 30–60s per turn).

### 3. Per-provider context windows

`usage.py:_CONTEXT_WINDOWS` defines the denominator:

| Provider | Window |
|----------|--------|
| claude   | 1,000,000 tokens |
| gemini   | 1,000,000 tokens |
| codex    |   200,000 tokens |

`estimate_context_usage` divides `last_turn_input_tokens` by the right window, so a Codex worker at 90% sees 180k tokens while a Claude worker at 90% sees 900k. **Phase 1 doesn't need to special-case providers** — `worker.context_pct` is already provider-aware.

### 4. Threshold configuration: already there

`config/models.py:129–130`:
```python
context_warning_threshold: float = 0.7
context_critical_threshold: float = 0.9
```

These match the Phase 1 plan exactly (70% / 90%). Loader honors `swarm.yaml` overrides via `config_loader.py:327–328`.

### 5. Hysteresis: already there

`SwarmDaemon._check_context_pressure` (daemon.py:1021) maintains a per-worker `_ctx_levels: dict[str, str]` and only emits a notification **on level change**, not every tick. Worker bouncing between 69% and 71% emits one warning, not 240 warnings/hour.

### 6. Notifications: already wired

`NotificationBus.emit_context_pressure(worker, usage_pct, level)` (notify/bus.py:200) emits a `CONTEXT_PRESSURE` event with `Severity.WARNING` for warning level and `Severity.URGENT` for critical. Plumbed into the dashboard / notification channels.

---

## What's missing — Phase 1's actual scope

The notifications fire but **nothing acts on them.** The "do something" half is unimplemented. Phase 1 adds:

1. **A new drone** (`src/swarm/drones/context_pressure.py`) that subscribes to the same threshold-crossing logic OR polls `worker.context_pct` on the existing 15s cycle.
2. **Soft action (≥ warning_threshold, < critical_threshold):**
   - Queue a `/compact` prompt for next IdleWatcher tick when worker is RESTING.
   - Skip if already queued (debounce by worker+threshold).
3. **Hard action (≥ critical_threshold):**
   - WAITING → defer; log `compact_deferred_for_waiting`; re-attempt on next tick.
   - BUZZING → send Ctrl-C, then inject `/compact`. Log the interrupt.
   - RESTING → direct `/compact` injection.
4. **Buzz-log integration.** Use existing `LogCategory.PRESSURE` (already exists for memory pressure) — no new category needed unless Phase 1 review wants the visual separation.
5. **Dashboard worker-card chip** showing the current pressure tier (`normal` / `soft` / `hard`).

The drone reads `worker.context_pct` (not raw usage), respects the existing config knobs, and can reuse `_check_context_pressure`'s level-transition logic by sharing the `_ctx_levels` dict OR by listening to `notification_bus` events.

---

## Reliability considerations

| Concern | Status |
|---------|--------|
| Session JSONL absent (worker just spawned, no turns yet) | `read_session_usage` returns empty `TokenUsage`; `context_pct = 0`. Safe — drone won't trigger. |
| Session JSONL stale after worker reload | `find_active_session` always returns most-recent JSONL regardless of mtime — this was deliberately hardened (`usage.py:69–98`). |
| Usage doesn't reflect just-compacted state | `last_turn_input_tokens` updates as soon as the next assistant turn writes to the JSONL. Worst case: 1 turn lag, ~30s. |
| Multiple sessions in same project dir | `find_active_session` picks most-recent; correct for the active session even with old transcripts on disk. |
| Worker process killed mid-turn | `last_turn_input_tokens` reflects last completed turn; `context_pct` may briefly overestimate. Acceptable — drone defers on STUNG state anyway. |
| Provider not in `_CONTEXT_WINDOWS` | Falls back to claude (1M). Conservative — won't over-trigger. |

No reliability gaps require Phase 0 to switch data sources.

---

## Recommendation

**Phase 1 design: drone-only.** Don't touch `usage.py`, `_usage_refresh_loop`, or `_check_context_pressure`. Add `src/swarm/drones/context_pressure.py` that subscribes to the existing pressure signal and implements the soft/hard action ladder. Wire it from `drones/pilot.py`.

Estimated Phase 1 scope:
- New: `src/swarm/drones/context_pressure.py` (~150 LOC + tests)
- Modify: `src/swarm/drones/pilot.py` (~5 LOC — register watcher)
- Modify: `src/swarm/web/templates/dashboard.html` worker card (pressure-tier chip)
- Tests: drone behavior under each (state × tier) combination

No new dependencies. No schema changes. No data-source migration.
