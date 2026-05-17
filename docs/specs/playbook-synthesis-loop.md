---
title: "Playbook synthesis loop — self-improving procedural memory"
status: shipped
proposed_date: 2026-05-17
shipped_date: 2026-05-17
related_specs:
  - headless-queen-architecture.md
---

> **Shipped 2026-05-17** — Phase 1 (`2026.5.17`), Phase 2 (`2026.5.17.2`),
> Phase 3 (`2026.5.17.4`), Phase 4 (this release). All four phases
> implemented + tested. The only deliberately deferred item is
> operator-editability of `PlaybookConfig` via the dashboard /
> `config_store` round-trip (see Phase 4 note below) — every other knob
> ships with sane `HiveConfig`/`swarm.yaml` defaults.

# Playbook synthesis loop (proposed)

Mine **successful** completed work into reusable, self-improving
procedures ("playbooks") that propagate across the worker fleet, so
workers stop re-deriving solved approaches. Inspired by Hermes Agent's
self-improving skill loop (Nous Research), re-scoped to Swarm's
true-multi-agent + Claude-Code-subscription architecture.

## TL;DR

- Swarm's memory today is **reactive**: `swarm_save_learning` /
  `queen_save_learning` capture operator *corrections* (things that went
  wrong). Nothing mines the far larger stream of things that went
  *right*.
- On task ship, the **headless Queen** decides whether the task encoded a
  generalizable procedure; if so it emits/updates a **playbook**.
- Playbooks are deduped, scored by real outcomes, consolidated, pruned,
  recalled at task-dispatch, and (for Claude workers) installed as
  native `.claude/skills/` Skills via the existing hooks installer.
- Subscription-safe (headless `claude -p`, not metered API), volume-
  bounded, provider-neutral, additive — it does not replace reactive
  learnings.

## Naming — three different things called "skill"

This is load-bearing; do not conflate:

| Term | What it is | Where |
|---|---|---|
| **`SkillsStore` / `skills` table** | EXISTING. Registry of slash-command workflows (`/fix-and-ship` → task types) + usage counts. | `src/swarm/db/skills_store.py` |
| **Claude Code Skills** | The `.claude/skills/<name>/SKILL.md` artifacts (`swarm-checkpoint`, etc.). A *delivery format*. | installed by `src/swarm/hooks/install.py` |
| **Playbooks** (this spec) | NEW. Learned, self-improving procedures synthesized from successful tasks. | new `playbooks` table / `PlaybookStore` |

Playbooks are a distinct concept with a distinct table and store. They
may be *delivered as* Claude Code Skills, but must not overload the
existing `skills` table or `SkillsStore`.

## Why the headless Queen (not interactive, not metered API)

Per `docs/specs/headless-queen-architecture.md`: drone-driven, high-
frequency, parallel decisions go to the headless Queen. Synthesis fires
on task completion (tens/day), each an independent
`claude -p` subprocess — subscription-safe, no per-token billing.
Pressure-tested against a deterministic rule: deciding "is this
generalizable, what are the steps/pitfalls, is it a dup of an existing
playbook" genuinely needs context reasoning, so it warrants a Queen
call — but it is hard-gated (eligible task types, memoization, per-hour
cap) so it adds bounded volume on top of the ~104 decisions/day baseline.

## Data model

New `playbooks` table (schema migration — follow the existing versioned
migration pattern in `src/swarm/db/core.py`, the v6/v7/v8 lineage named
in `CLAUDE.md`; this is the next version). `PlaybookStore(BaseStore)` in
`src/swarm/db/playbook_store.py`, mirroring `QueenChatStore` /
`BuzzStore` (`src/swarm/db/base_store.py:15`).

`playbooks`:

| column | type | notes |
|---|---|---|
| `id` | text pk | |
| `name` | text unique | kebab slug |
| `title` | text | one line |
| `scope` | text | `global` \| `project:<repo>` \| `worker:<name>` |
| `trigger` | text | when-to-use (FTS-indexed) |
| `body` | text | markdown: steps + pitfalls (FTS-indexed) |
| `provenance_task_ids` | json | source task numbers |
| `source_worker` | text | first synthesizer |
| `confidence` | real | Queen's synthesis confidence |
| `uses` / `wins` / `losses` | int | outcome attribution |
| `status` | text | `candidate` \| `active` \| `retired` |
| `version` | int | bumped on consolidation |
| `content_hash` | text | normalized-body hash, exact-dup guard |
| `created_at` / `updated_at` / `last_used_at` | real | |
| `retired_reason` | text | audit |

Plus `playbooks_fts` (SQLite FTS5 over `title`/`trigger`/`body`) for
recall + near-dup detection, and a `playbook_events` table
(`id, playbook_id, task_id, worker, event, ts, detail`) where `event` ∈
`synthesized|consolidated|applied|win|loss|promoted|retired` — the audit
+ refinement signal.

## Components (new + reused)

New package `src/swarm/playbooks/`:

- `models.py` — `Playbook`, `PlaybookScope`, `PlaybookStatus`
  dataclasses/enums.
- `synthesizer.py` — `PlaybookSynthesizer`: builds the headless-Queen
  prompt from a shipped task (number, type, title, description,
  resolution, files-touched summary, key buzz/message context), parses
  the JSON verdict, writes/updates via `PlaybookStore`. Owns
  memoization + per-hour cap.
- `installer.py` — renders `active`, in-scope playbooks to
  `.claude/skills/pb-<name>/SKILL.md` (Claude Code Skill format) and is
  invoked from the existing per-worker artifact pass. Provider-neutral
  fallback: non-Claude workers get playbooks via MCP recall only.

Reused (do not reinvent):

- `src/swarm/db/playbook_store.py` extends `BaseStore`
  (`src/swarm/db/base_store.py:15`) — same pattern as `QueenChatStore`.
- `src/swarm/queen/queen.py` — reuse `ask()` (`queen.py:294`,
  `_DEFAULT_TIMEOUT` 120s). Add a decision shape to
  `HEADLESS_DECISION_PROMPT` (`queen.py:31`) — a new numbered shape
  "Playbook synthesis" alongside auto-assignment/oversight/completion.
- `src/swarm/server/daemon.py` — hook `complete_task()`
  (`daemon.py:2029`) at the **post-ship** site (the same place the
  task #225 Phase 3 self-loop fires, ~`daemon.py:2122`). Enqueue a
  synthesis call fire-and-forget through the existing bg-task tracking
  / Queen queue; never block the ship path.
- `src/swarm/hooks/install.py` — extend the per-worker artifact
  installer (reached via `daemon._install_worker_artifacts()`
  `daemon.py:1268`; see `install_worker_commands` `install.py:364` and
  the `SKILL.md` machinery at `install.py:242`). Re-render on playbook
  activation.
- `src/swarm/drones/log.py` — add `DroneAction` members
  `PLAYBOOK_SYNTHESIZED`, `PLAYBOOK_CONSOLIDATED`, `PLAYBOOK_RETIRED`,
  `PLAYBOOK_APPLIED`; log under `LogCategory.DRONE`.
- `src/swarm/config/models.py` — add `@dataclass class PlaybookConfig`
  (pattern of `DroneConfig`/`QueenConfig`, lines 101/198), seeded into
  config like the others.
- `assign_and_start_task` / the task-push dispatch path
  (`daemon.py:2016`) — inject the top-N FTS-relevant active playbooks
  for the task's scope into the dispatch message (recall-at-start, the
  Hermes session-start-injection analog).
- MCP: new `swarm_get_playbooks(query?, scope?, limit?)` worker tool +
  `swarm_record_playbook_outcome(name, outcome)` in
  `src/swarm/mcp/tools.py`; surfaced live per `CLAUDE.md` "Live MCP
  tool-surface propagation". Optional `queen_consolidate_playbooks`
  in `queen_tools.py`.

## Lifecycle

```
ship task ──► headless Queen: generalizable?
                 │ no → nothing
                 │ yes → dedupe (content_hash / FTS near-match)
                 │        ├─ exact dup → bump uses, append provenance
                 │        ├─ near dup  → consolidate into existing (version++)
                 │        └─ novel     → create  status=candidate
recall (dispatch injection / swarm_get_playbooks) ─► playbook_events 'applied'
task outcome (Swarm's existing headless completion verification,
   v8 task-verification fields) ─► attribute win/loss to applied playbooks
auto-promote: uses ≥ N and winrate ≥ X  ─► status=active ─► installer
prune: uses ≥ N and winrate < Y          ─► status=retired (reason)
periodic consolidation sweep: merge FTS-near actives, retire losers
```

Only `active` playbooks are installed/recalled fleet-wide. `candidate`
playbooks are visible to the Queen and dashboard but not pushed to
workers — unvetted procedure does not propagate.

## Volume discipline (subscription safety)

- Synthesize only for eligible task types (`feature|bug|chore`, not
  `verify`/no-op) **and** non-trivial resolution (files-touched > 0,
  resolution length ≥ `min_resolution_chars`).
- One synthesis attempt per `(worker, task)` — memoized.
- `PlaybookConfig.max_synth_per_hour` (default 20) — beyond it, drop
  with a `buzz_log` note rather than queue unboundedly.
- Consolidation/prune sweep is low-frequency (every
  `consolidation_interval_seconds`, default 6h, or after K new
  candidates) — reuse an existing periodic loop cadence; do not add a
  tight poll loop.
- Every Queen call logged to `buzz_log` (category `DRONE`) for the same
  auditability the headless-Queen spec relies on.

## `PlaybookConfig`

`enabled` (default true), `eligible_task_types`, `min_resolution_chars`,
`max_synth_per_hour`, `auto_promote_uses`, `auto_promote_winrate`,
`prune_min_uses`, `prune_max_winrate`, `consolidation_interval_seconds`,
`dedupe_similarity_threshold`, `install_as_native_skills` (bool,
provider-aware).

## Provider neutrality

Native `.claude/skills/` install only for Claude workers (the installer
already branches per provider). Gemini/Codex/OpenCode workers still get
synthesized playbooks via `swarm_get_playbooks` + dispatch injection.
No Claude-only branding (`feedback_provider_neutral`).

## Phases (ship independently — `feedback_ship_phases_independently`)

1. **Store + synth core.** Migration + `PlaybookStore` + FTS +
   `PlaybookSynthesizer` + `HEADLESS_DECISION_PROMPT` shape +
   `complete_task` hook + buzz logging + `swarm_get_playbooks` MCP +
   tests. Playbooks stored & recallable; nothing auto-pushed.
2. **Outcome loop.** Dispatch-injection recall + win/loss attribution
   off the existing completion verification + auto-promote + prune.
3. **Propagation.** `installer.py` → `.claude/skills/` for Claude
   workers + consolidation sweep.
4. **Operator surface.** Dashboard "Playbooks" tab (list, confidence,
   uses/winrate, provenance, promote/retire) + read routes in
   `src/swarm/server/routes/`.

Each phase is its own `release: X.Y.Z`.

## Tests (TDD, per phase)

- `PlaybookStore`: CRUD, FTS search ranking, exact-dup rejection
  (content_hash), near-dup routing, winrate math, promote/prune/
  consolidation queries, migration idempotent.
- `PlaybookSynthesizer`: prompt-builder shape; parses valid Queen JSON;
  rejects junk; ineligible task types skipped; firing twice within the
  window yields one call (memoization); `max_synth_per_hour` enforced.
- `complete_task` hook: ≤1 synthesis per `(worker, task)`; never blocks
  ship; survives Queen error/timeout.
- `installer.py`: valid `SKILL.md`; idempotent; only `active` + in
  scope; provider gating.
- MCP: `swarm_get_playbooks` scoped FTS ranking;
  `swarm_record_playbook_outcome` updates stats + logs `playbook_events`.
- Prompt: `HEADLESS_DECISION_PROMPT` carries the synthesis contract
  (mirror `tests/test_headless_decision_prompt.py`).
- E2E: ship task → candidate → similar task recalls it → outcome →
  auto-promote → installed.

## Non-goals

- Not metered API; not a tight new poll loop (a `complete_task` hook +
  cooldown + a low-freq sweep suffice).
- Not provider-locked.
- Does **not** replace reactive learnings — `swarm_save_learning` stays
  (corrections); playbooks are successes. Bodies may cross-link to
  learnings.
- Does not overload the existing `skills` table / `SkillsStore`.
- Candidate (unvetted) playbooks are never pushed fleet-wide.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| LLM volume creep | eligible-type gate + memoization + `max_synth_per_hour` + headless-only |
| Playbook slop propagating | candidate→active gate (confidence + uses + winrate), dedupe, consolidation, operator retire, full provenance |
| Stale procedures as code changes | winrate decay + prune; FTS recall keeps irrelevant playbooks from surfacing; provenance lets the Queen re-evaluate |
| Repo-specific steps leaking fleet-wide | `scope` (`global`/`project:<repo>`/`worker:`); recall + install respect scope |
| Naming confusion | distinct `playbooks` table/store/tool; this section is normative |

## Open questions

- Promote threshold defaults (`auto_promote_uses`/`winrate`) — start
  conservative (e.g. uses ≥ 3, winrate ≥ 0.7) and tune from
  `playbook_events` data, mirroring how the completion-backoff numbers
  were tuned in the headless-Queen spec.
- Whether consolidation should ever auto-merge across `scope` (default:
  no — only within the same scope).

## Phase 4 closeout (shipped 2026.5.17.4 / this release; swarm #404)

Delivered:

- `src/swarm/server/routes/playbooks.py` — `GET /api/playbooks`
  (all statuses incl. candidates, optional `?status=`/`?scope=`) +
  `POST /api/playbooks/{name}/promote` + `.../retire` (body `reason`).
  Same global auth/CSRF middleware as every other `/api` route;
  registered via `routes/register_all`. Route tests in
  `tests/test_playbook_routes.py`.
- Dashboard **Playbooks** bottom-tab (`dashboard.html` +
  `dashboard.js`): active-first list with status badge (active /
  **candidate** / retired visually distinct), winrate / uses /
  provenance / scope / trigger, and operator Promote (candidates) /
  Retire controls wired to the routes.
- Spec frontmatter flipped `proposed → shipped`.

**Deferred, by decision (acceptance-criterion option B):**
operator-editability of `PlaybookConfig` via the dashboard /
`config_store` DB round-trip is **NOT** implemented. Rationale: the
config-save chain is audited/sensitive
(`docs/audits/config-save-chain-2026-05-04.md`,
`reference_config_save_audit`); threading a new sub-config through
loader + `config_store` + serialization + the config-manager UI is a
disproportionate, regression-prone change for a tuning surface that
already has sane `HiveConfig`/`swarm.yaml` defaults and is rarely
retuned. Filed as a future task if/when an operator needs live
retuning. This was a stated implementer's-call in the task; choosing
explicit deferral over destabilizing the config chain.
