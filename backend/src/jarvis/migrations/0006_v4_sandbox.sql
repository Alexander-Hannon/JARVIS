-- V4 sandbox: one disposable Docker workspace. Network egress opens only through an approved, expiring domain window.
CREATE TABLE sandbox_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  net_domains TEXT NOT NULL DEFAULT '[]',
  net_until TEXT,
  started_at TEXT,
  last_used_at TEXT
);
INSERT INTO sandbox_state (id) VALUES (1);
CREATE TABLE sandbox_runs (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  kind TEXT NOT NULL,              -- exec | write | read | browse | computer | network | lifecycle
  detail TEXT NOT NULL,            -- command / path / url / action (model-supplied; shown to AJ in the Workbench)
  exit_code INTEGER,
  output TEXT,
  duration_ms INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX sandbox_runs_at ON sandbox_runs(created_at);
