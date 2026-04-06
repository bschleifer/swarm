"""Database schema definitions for swarm.db.

All table creation SQL lives here.  The schema version is tracked in
the ``schema_version`` table so future migrations can be applied
incrementally.
"""

from __future__ import annotations

CURRENT_VERSION = 3

PRAGMAS = """\
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
"""

SCHEMA_V1 = """\
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER PRIMARY KEY,
  applied_at  REAL    NOT NULL
);

-- ============================================================
-- CONFIG
-- ============================================================

CREATE TABLE IF NOT EXISTS config (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  updated_at  REAL
);

CREATE TABLE IF NOT EXISTS workers (
  id          TEXT PRIMARY KEY,
  name        TEXT UNIQUE NOT NULL,
  path        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  provider    TEXT NOT NULL DEFAULT '',
  isolation   TEXT NOT NULL DEFAULT '',
  identity    TEXT NOT NULL DEFAULT '',
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  REAL
);

CREATE TABLE IF NOT EXISTS groups (
  id    TEXT PRIMARY KEY,
  name  TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS group_workers (
  group_id    TEXT    NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  worker_id   TEXT    NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (group_id, worker_id)
);

CREATE TABLE IF NOT EXISTS config_overrides (
  id          INTEGER PRIMARY KEY,
  owner_type  TEXT NOT NULL,
  owner_id    TEXT,
  key         TEXT NOT NULL,
  value       TEXT,
  UNIQUE(owner_type, owner_id, key)
);

CREATE TABLE IF NOT EXISTS approval_rules (
  id          INTEGER PRIMARY KEY,
  owner_type  TEXT NOT NULL DEFAULT 'global',
  owner_id    TEXT,
  pattern     TEXT NOT NULL,
  action      TEXT NOT NULL,
  sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_approval_rules_owner ON approval_rules(owner_type, owner_id);

-- ============================================================
-- TASKS
-- ============================================================

CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  number            INTEGER UNIQUE,
  title             TEXT NOT NULL,
  description       TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'pending',
  priority          TEXT NOT NULL DEFAULT 'normal',
  task_type         TEXT NOT NULL DEFAULT 'chore',
  assigned_worker   TEXT,
  created_at        REAL,
  updated_at        REAL,
  completed_at      REAL,
  resolution        TEXT NOT NULL DEFAULT '',
  tags              TEXT NOT NULL DEFAULT '[]',
  attachments       TEXT NOT NULL DEFAULT '[]',
  depends_on        TEXT NOT NULL DEFAULT '[]',
  source_email_id   TEXT,
  jira_key          TEXT,
  is_cross_project  INTEGER NOT NULL DEFAULT 0,
  source_worker     TEXT,
  target_worker     TEXT,
  dependency_type   TEXT,
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',
  context_refs      TEXT NOT NULL DEFAULT '[]',
  cost_budget       REAL,
  cost_spent        REAL NOT NULL DEFAULT 0,
  learnings         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_worker);
CREATE INDEX IF NOT EXISTS idx_tasks_jira ON tasks(jira_key);

CREATE TABLE IF NOT EXISTS task_history (
  id          INTEGER PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  action      TEXT NOT NULL,
  actor       TEXT,
  detail      TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_history_task ON task_history(task_id);
CREATE INDEX IF NOT EXISTS idx_task_history_time ON task_history(created_at);

-- ============================================================
-- PROPOSALS
-- ============================================================

CREATE TABLE IF NOT EXISTS proposals (
  id                TEXT PRIMARY KEY,
  worker_name       TEXT,
  task_id           TEXT,
  task_title         TEXT,
  proposal_type     TEXT,
  status            TEXT NOT NULL DEFAULT 'pending',
  confidence        REAL,
  assessment        TEXT,
  message           TEXT,
  reasoning         TEXT,
  queen_action      TEXT,
  prompt_snippet    TEXT,
  rule_pattern      TEXT,
  is_plan           INTEGER NOT NULL DEFAULT 0,
  rejection_reason  TEXT,
  created_at        REAL,
  resolved_at       REAL
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_worker ON proposals(worker_name);
CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(task_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status_time ON proposals(status, created_at);

-- ============================================================
-- BUZZ LOG
-- ============================================================

CREATE TABLE IF NOT EXISTS buzz_log (
  id              INTEGER PRIMARY KEY,
  timestamp       REAL NOT NULL,
  action          TEXT NOT NULL,
  worker_name     TEXT,
  detail          TEXT NOT NULL DEFAULT '',
  category        TEXT NOT NULL DEFAULT 'drone',
  is_notification INTEGER NOT NULL DEFAULT 0,
  metadata        TEXT NOT NULL DEFAULT '{}',
  repeat_count    INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_buzz_timestamp ON buzz_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_buzz_worker ON buzz_log(worker_name);
CREATE INDEX IF NOT EXISTS idx_buzz_action ON buzz_log(action);
CREATE INDEX IF NOT EXISTS idx_buzz_category ON buzz_log(category);
CREATE INDEX IF NOT EXISTS idx_buzz_worker_time ON buzz_log(worker_name, timestamp);

-- ============================================================
-- MESSAGES
-- ============================================================

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY,
  sender      TEXT NOT NULL,
  recipient   TEXT NOT NULL,
  msg_type    TEXT NOT NULL,
  content     TEXT NOT NULL,
  created_at  REAL NOT NULL,
  read_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(recipient, read_at);

-- ============================================================
-- PIPELINES
-- ============================================================

CREATE TABLE IF NOT EXISTS pipelines (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  schedule    TEXT,
  config      TEXT NOT NULL DEFAULT '{}',
  created_at  REAL,
  updated_at  REAL
);

CREATE TABLE IF NOT EXISTS pipeline_stages (
  id            INTEGER PRIMARY KEY,
  pipeline_id   TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
  stage_order   INTEGER NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  action        TEXT NOT NULL,
  config        TEXT NOT NULL DEFAULT '{}',
  UNIQUE(pipeline_id, stage_order)
);

-- ============================================================
-- SECRETS
-- ============================================================

CREATE TABLE IF NOT EXISTS secrets (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  REAL
);

-- ============================================================
-- QUEEN SESSIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS queen_sessions (
  name        TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  created_at  REAL
);
"""
