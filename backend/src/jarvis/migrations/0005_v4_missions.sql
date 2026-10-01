-- V4 missions: standing goals JARVIS works on in the background ("heartbeat"), created by AJ in the UI.
CREATE TABLE missions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  goal TEXT NOT NULL,                       -- AJ's own words: what to check / do
  schedule TEXT NOT NULL,                   -- JSON {"kind": "daily"|"weekly"|"interval", "at": "HH:MM", "days": [0-6], "minutes": N}
  tz TEXT NOT NULL,
  families TEXT NOT NULL DEFAULT '[]',      -- JSON: tool families the mission may use ([] = read-only planning tools)
  allow_proposals INTEGER NOT NULL DEFAULT 0,
  output TEXT NOT NULL DEFAULT 'notify' CHECK (output IN ('notify','quiet')), -- quiet = only notify when something is found
  max_cost_usd REAL NOT NULL DEFAULT 0.25,  -- per run
  monthly_cost_usd REAL NOT NULL DEFAULT 5.0,
  quiet_start TEXT,                         -- "22:00": no push between quiet_start and quiet_end (still in the inbox)
  quiet_end TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  conversation_id TEXT,
  next_run_at TEXT,
  last_run_at TEXT,
  last_status TEXT,
  last_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE mission_runs (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  run_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,                     -- running | done | nothing_new | failed | skipped_budget
  summary TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  proposal_id TEXT
);
CREATE INDEX mission_runs_mission ON mission_runs(mission_id, started_at);
