-- V4 tiered autonomy.
-- Policy rules are authored by AJ through the authenticated UI only; the model has no path that writes them.
CREATE TABLE autonomy_rules (
  id TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  label TEXT NOT NULL,
  match TEXT NOT NULL DEFAULT '{}',          -- JSON: {"target_resource": "...", "args": {"key": "value"}} (all must equal)
  origins TEXT NOT NULL DEFAULT '["command"]', -- which proposal origins the rule may approve (command|mission|channel)
  daily_cap INTEGER NOT NULL DEFAULT 10,
  created_at TEXT NOT NULL,
  created_session TEXT,
  expires_at TEXT,
  revoked_at TEXT
);
CREATE INDEX autonomy_rules_type ON autonomy_rules(action_type);

CREATE TABLE policy_decisions (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL REFERENCES autonomy_rules(id),
  proposal_id TEXT NOT NULL REFERENCES proposals(id),
  approval_id TEXT NOT NULL,
  action_count INTEGER NOT NULL,
  decided_at TEXT NOT NULL
);
CREATE INDEX policy_decisions_rule ON policy_decisions(rule_id, decided_at);

-- Where a proposal came from decides which rules may auto-approve it: untrusted-content-driven work (missions reading
-- email, channel messages) never rides on a rule that AJ only granted for his own direct commands.
ALTER TABLE proposals ADD COLUMN origin TEXT NOT NULL DEFAULT 'command';
ALTER TABLE proposed_actions ADD COLUMN undo_until TEXT;
ALTER TABLE proposed_actions ADD COLUMN undone_by TEXT;

-- Widen approval channels (SQLite cannot alter a CHECK; rebuild — nothing references approvals by FK).
CREATE TABLE approvals_v4 (
  id TEXT PRIMARY KEY,
  proposal_version_id TEXT NOT NULL REFERENCES proposal_versions(id),
  proposal_hash TEXT NOT NULL,
  selected_items TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('button','voice','typed','policy','telegram','discord')),
  session_id TEXT REFERENCES sessions(id),
  evidence TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
INSERT INTO approvals_v4 SELECT id, proposal_version_id, proposal_hash, selected_items, channel, session_id, evidence,
  created_at, revoked_at FROM approvals;
DROP TABLE approvals;
ALTER TABLE approvals_v4 RENAME TO approvals;

-- Widen audit actors so the Activity log can say who acted: AJ, a policy rule, a mission, or a messaging channel.
CREATE TABLE audit_events_v4 (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('user','system','model','worker','policy','mission','channel')),
  kind TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  summary TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO audit_events_v4 SELECT id, at, actor, kind, entity_type, entity_id, summary, data FROM audit_events;
DROP TABLE audit_events;
ALTER TABLE audit_events_v4 RENAME TO audit_events;
CREATE INDEX audit_at ON audit_events(at);
