-- jarvis:foreign-keys-off
-- V5 M1a per-user data. Private by default: every per-user root row carries user_id; child rows (messages,
-- proposal_versions, memory_versions, …) are scoped through their parent. Existing rows belong to the first owner.
-- Tables whose UNIQUE must include the user are rebuilt (foreign keys are off for this migration so DROP TABLE does
-- not cascade into children; migrate() runs foreign_key_check before COMMIT).
ALTER TABLE scope_grants ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE scope_grants SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX scope_grants_user ON scope_grants(user_id);
ALTER TABLE conversations ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE conversations SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX conversations_user ON conversations(user_id);
ALTER TABLE voice_sessions ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE voice_sessions SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX voice_sessions_user ON voice_sessions(user_id);
ALTER TABLE captures ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE captures SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX captures_user ON captures(user_id);
ALTER TABLE runs ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE runs SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX runs_user ON runs(user_id);
ALTER TABLE source_documents ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE source_documents SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX source_documents_user ON source_documents(user_id);
ALTER TABLE memories ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE memories SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX memories_user ON memories(user_id);
ALTER TABLE proposals ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE proposals SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX proposals_user ON proposals(user_id);
ALTER TABLE autonomy_rules ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE autonomy_rules SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX autonomy_rules_user ON autonomy_rules(user_id);
ALTER TABLE missions ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE missions SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX missions_user ON missions(user_id);
ALTER TABLE briefings ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE briefings SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX briefings_user ON briefings(user_id);
ALTER TABLE debriefs ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE debriefs SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX debriefs_user ON debriefs(user_id);
ALTER TABLE notifications ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE notifications SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX notifications_user ON notifications(user_id);
ALTER TABLE push_subscriptions ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE push_subscriptions SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX push_subscriptions_user ON push_subscriptions(user_id);
ALTER TABLE task_links ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE task_links SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX task_links_user ON task_links(user_id);
ALTER TABLE calendar_links ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE calendar_links SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX calendar_links_user ON calendar_links(user_id);
ALTER TABLE usage_reservations ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE usage_reservations SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX usage_reservations_user ON usage_reservations(user_id);
ALTER TABLE sandbox_runs ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE sandbox_runs SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX sandbox_runs_user ON sandbox_runs(user_id);
ALTER TABLE integration_accounts ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE integration_accounts SET user_id=(SELECT id FROM users WHERE id='usr_owner');
CREATE INDEX integration_accounts_user ON integration_accounts(user_id);
-- Org-level integration accounts (the Anthropic key, the Telegram bot) have no user.
UPDATE integration_accounts SET user_id=NULL WHERE provider IN ('anthropic','telegram');
-- Single-slot providers used fixed external ids; they become per user so two people can each have one.
UPDATE integration_accounts SET external_account_id = external_account_id || ':' || user_id
  WHERE provider IN ('ics','github','routing','simplefin') AND external_account_id IN ('ics-feed','github','ors','simplefin')
    AND user_id IS NOT NULL;
-- AJ's paired Telegram chat becomes his channel link (the bot itself is org-level).
INSERT INTO channel_links (id, user_id, channel, chat_id, chat_name, conversation_id, paired_at)
  SELECT 'chl_owner', u.id, 'telegram', CAST(json_extract(a.config, '$.chat_id') AS TEXT), json_extract(a.config, '$.chat_name'),
         json_extract(a.config, '$.conversation_id'), a.updated_at
  FROM integration_accounts a JOIN users u ON u.id='usr_owner'
  WHERE a.provider='telegram' AND a.status!='revoked' AND json_extract(a.config, '$.chat_id') IS NOT NULL;
-- Documents can be shared with the whole org (read-only for others); everything else stays private.
ALTER TABLE source_documents ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','org'));

-- user_settings: one row per user (personal preferences). Org keys (budget, models, brand) moved to org_settings.
CREATE TABLE user_settings_v (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  data TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
INSERT INTO user_settings_v (user_id, timezone, data, version, updated_at)
  SELECT u.id, s.timezone, json_remove(s.data, '$.budget', '$.models', '$.brand'), s.version, s.updated_at
  FROM user_settings s JOIN users u ON u.id='usr_owner' WHERE s.id=1;
DROP TABLE user_settings;
ALTER TABLE user_settings_v RENAME TO user_settings;

CREATE TABLE daily_plans_v (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  plan_date TEXT NOT NULL,           -- local date in the user's tz
  current_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (user_id, plan_date)
);
INSERT INTO daily_plans_v SELECT d.id, u.id, d.plan_date, d.current_version, d.created_at
  FROM daily_plans d JOIN users u ON u.id='usr_owner';
DROP TABLE daily_plans;
ALTER TABLE daily_plans_v RENAME TO daily_plans;

CREATE TABLE schedules_v (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  key TEXT NOT NULL,
  workflow TEXT NOT NULL,           -- daily_command | nightly_debrief | weekly_war_room
  label TEXT NOT NULL,
  tz TEXT NOT NULL,
  hour INTEGER NOT NULL,
  minute INTEGER NOT NULL,
  weekdays TEXT NOT NULL,           -- JSON list 0=Mon..6=Sun
  enabled INTEGER NOT NULL DEFAULT 0,
  misfire_grace_minutes INTEGER NOT NULL DEFAULT 90,
  destination TEXT NOT NULL DEFAULT 'inbox',   -- inbox | inbox+push
  next_run_at TEXT,
  last_run_at TEXT,
  last_status TEXT,
  last_success_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  row_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (user_id, key)
);
INSERT INTO schedules_v SELECT s.id, u.id, s.key, s.workflow, s.label, s.tz, s.hour, s.minute, s.weekdays, s.enabled,
  s.misfire_grace_minutes, s.destination, s.next_run_at, s.last_run_at, s.last_status, s.last_success_at, s.created_at,
  s.updated_at, s.row_version FROM schedules s JOIN users u ON u.id='usr_owner';
DROP TABLE schedules;
ALTER TABLE schedules_v RENAME TO schedules;

CREATE TABLE course_mappings_v (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  pattern TEXT NOT NULL,            -- case-insensitive substring, e.g. "IS 640"
  course TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('own','gta')),
  confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (user_id, pattern)
);
INSERT INTO course_mappings_v SELECT c.id, u.id, c.pattern, c.course, c.role, c.confirmed, c.created_at
  FROM course_mappings c JOIN users u ON u.id='usr_owner';
DROP TABLE course_mappings;
ALTER TABLE course_mappings_v RENAME TO course_mappings;

CREATE TABLE projects_v (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  slug TEXT NOT NULL,
  name TEXT NOT NULL,
  area TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  notion_url TEXT,
  sensitive INTEGER NOT NULL DEFAULT 0,  -- student/client material; never attached to unrelated runs
  visibility TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','org')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (user_id, slug)
);
INSERT INTO projects_v (id, user_id, slug, name, area, description, notion_url, sensitive, created_at, updated_at)
  SELECT p.id, u.id, p.slug, p.name, p.area, p.description, p.notion_url, p.sensitive, p.created_at, p.updated_at
  FROM projects p JOIN users u ON u.id='usr_owner';
DROP TABLE projects;
ALTER TABLE projects_v RENAME TO projects;
-- Seeded-but-never-claimed rows (an install started before anyone set it up) have no owner to belong to.
DELETE FROM memories WHERE project_id IS NOT NULL AND project_id NOT IN (SELECT id FROM projects);
UPDATE source_documents SET project_id=NULL WHERE project_id IS NOT NULL AND project_id NOT IN (SELECT id FROM projects);
DELETE FROM daily_plan_versions WHERE plan_id NOT IN (SELECT id FROM daily_plans);
DELETE FROM schedule_runs WHERE schedule_id NOT IN (SELECT id FROM schedules);

CREATE TABLE sandbox_state_v (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  net_domains TEXT NOT NULL DEFAULT '[]',
  net_until TEXT,
  started_at TEXT,
  last_used_at TEXT
);
INSERT INTO sandbox_state_v SELECT u.id, s.net_domains, s.net_until, s.started_at, s.last_used_at
  FROM sandbox_state s JOIN users u ON u.id='usr_owner';
DROP TABLE sandbox_state;
ALTER TABLE sandbox_state_v RENAME TO sandbox_state;

-- DB-level guard: a per-user row without an owner is a bug (a forgotten scope), so it fails loudly instead of
-- silently landing somewhere.
CREATE TRIGGER scope_grants_user_required BEFORE INSERT ON scope_grants WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'scope_grants.user_id is required'); END;
CREATE TRIGGER scope_grants_user_kept BEFORE UPDATE OF user_id ON scope_grants WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'scope_grants.user_id is required'); END;
CREATE TRIGGER conversations_user_required BEFORE INSERT ON conversations WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'conversations.user_id is required'); END;
CREATE TRIGGER conversations_user_kept BEFORE UPDATE OF user_id ON conversations WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'conversations.user_id is required'); END;
CREATE TRIGGER voice_sessions_user_required BEFORE INSERT ON voice_sessions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'voice_sessions.user_id is required'); END;
CREATE TRIGGER voice_sessions_user_kept BEFORE UPDATE OF user_id ON voice_sessions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'voice_sessions.user_id is required'); END;
CREATE TRIGGER captures_user_required BEFORE INSERT ON captures WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'captures.user_id is required'); END;
CREATE TRIGGER captures_user_kept BEFORE UPDATE OF user_id ON captures WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'captures.user_id is required'); END;
CREATE TRIGGER runs_user_required BEFORE INSERT ON runs WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'runs.user_id is required'); END;
CREATE TRIGGER runs_user_kept BEFORE UPDATE OF user_id ON runs WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'runs.user_id is required'); END;
CREATE TRIGGER source_documents_user_required BEFORE INSERT ON source_documents WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'source_documents.user_id is required'); END;
CREATE TRIGGER source_documents_user_kept BEFORE UPDATE OF user_id ON source_documents WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'source_documents.user_id is required'); END;
CREATE TRIGGER memories_user_required BEFORE INSERT ON memories WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'memories.user_id is required'); END;
CREATE TRIGGER memories_user_kept BEFORE UPDATE OF user_id ON memories WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'memories.user_id is required'); END;
CREATE TRIGGER proposals_user_required BEFORE INSERT ON proposals WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'proposals.user_id is required'); END;
CREATE TRIGGER proposals_user_kept BEFORE UPDATE OF user_id ON proposals WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'proposals.user_id is required'); END;
CREATE TRIGGER autonomy_rules_user_required BEFORE INSERT ON autonomy_rules WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'autonomy_rules.user_id is required'); END;
CREATE TRIGGER autonomy_rules_user_kept BEFORE UPDATE OF user_id ON autonomy_rules WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'autonomy_rules.user_id is required'); END;
CREATE TRIGGER missions_user_required BEFORE INSERT ON missions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'missions.user_id is required'); END;
CREATE TRIGGER missions_user_kept BEFORE UPDATE OF user_id ON missions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'missions.user_id is required'); END;
CREATE TRIGGER briefings_user_required BEFORE INSERT ON briefings WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'briefings.user_id is required'); END;
CREATE TRIGGER briefings_user_kept BEFORE UPDATE OF user_id ON briefings WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'briefings.user_id is required'); END;
CREATE TRIGGER debriefs_user_required BEFORE INSERT ON debriefs WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'debriefs.user_id is required'); END;
CREATE TRIGGER debriefs_user_kept BEFORE UPDATE OF user_id ON debriefs WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'debriefs.user_id is required'); END;
CREATE TRIGGER notifications_user_required BEFORE INSERT ON notifications WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'notifications.user_id is required'); END;
CREATE TRIGGER notifications_user_kept BEFORE UPDATE OF user_id ON notifications WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'notifications.user_id is required'); END;
CREATE TRIGGER push_subscriptions_user_required BEFORE INSERT ON push_subscriptions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'push_subscriptions.user_id is required'); END;
CREATE TRIGGER push_subscriptions_user_kept BEFORE UPDATE OF user_id ON push_subscriptions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'push_subscriptions.user_id is required'); END;
CREATE TRIGGER task_links_user_required BEFORE INSERT ON task_links WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'task_links.user_id is required'); END;
CREATE TRIGGER task_links_user_kept BEFORE UPDATE OF user_id ON task_links WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'task_links.user_id is required'); END;
CREATE TRIGGER calendar_links_user_required BEFORE INSERT ON calendar_links WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'calendar_links.user_id is required'); END;
CREATE TRIGGER calendar_links_user_kept BEFORE UPDATE OF user_id ON calendar_links WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'calendar_links.user_id is required'); END;
CREATE TRIGGER usage_reservations_user_required BEFORE INSERT ON usage_reservations WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'usage_reservations.user_id is required'); END;
CREATE TRIGGER usage_reservations_user_kept BEFORE UPDATE OF user_id ON usage_reservations WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'usage_reservations.user_id is required'); END;
CREATE TRIGGER sandbox_runs_user_required BEFORE INSERT ON sandbox_runs WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'sandbox_runs.user_id is required'); END;
CREATE TRIGGER sandbox_runs_user_kept BEFORE UPDATE OF user_id ON sandbox_runs WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'sandbox_runs.user_id is required'); END;
CREATE TRIGGER user_settings_user_required BEFORE INSERT ON user_settings WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'user_settings.user_id is required'); END;
CREATE TRIGGER user_settings_user_kept BEFORE UPDATE OF user_id ON user_settings WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'user_settings.user_id is required'); END;
CREATE TRIGGER daily_plans_user_required BEFORE INSERT ON daily_plans WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'daily_plans.user_id is required'); END;
CREATE TRIGGER daily_plans_user_kept BEFORE UPDATE OF user_id ON daily_plans WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'daily_plans.user_id is required'); END;
CREATE TRIGGER schedules_user_required BEFORE INSERT ON schedules WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'schedules.user_id is required'); END;
CREATE TRIGGER schedules_user_kept BEFORE UPDATE OF user_id ON schedules WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'schedules.user_id is required'); END;
CREATE TRIGGER course_mappings_user_required BEFORE INSERT ON course_mappings WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'course_mappings.user_id is required'); END;
CREATE TRIGGER course_mappings_user_kept BEFORE UPDATE OF user_id ON course_mappings WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'course_mappings.user_id is required'); END;
CREATE TRIGGER projects_user_required BEFORE INSERT ON projects WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'projects.user_id is required'); END;
CREATE TRIGGER projects_user_kept BEFORE UPDATE OF user_id ON projects WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'projects.user_id is required'); END;
CREATE TRIGGER sandbox_state_user_required BEFORE INSERT ON sandbox_state WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'sandbox_state.user_id is required'); END;
CREATE TRIGGER sandbox_state_user_kept BEFORE UPDATE OF user_id ON sandbox_state WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'sandbox_state.user_id is required'); END;
CREATE TRIGGER pairing_codes_user_required BEFORE INSERT ON pairing_codes WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'pairing_codes.user_id is required'); END;
CREATE TRIGGER pairing_codes_user_kept BEFORE UPDATE OF user_id ON pairing_codes WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'pairing_codes.user_id is required'); END;
CREATE TRIGGER oauth_states_user_required BEFORE INSERT ON oauth_states WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'oauth_states.user_id is required'); END;
CREATE TRIGGER oauth_states_user_kept BEFORE UPDATE OF user_id ON oauth_states WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'oauth_states.user_id is required'); END;
CREATE TRIGGER sessions_user_required BEFORE INSERT ON sessions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'sessions.user_id is required'); END;
CREATE TRIGGER sessions_user_kept BEFORE UPDATE OF user_id ON sessions WHEN NEW.user_id IS NULL
BEGIN SELECT RAISE(ABORT, 'sessions.user_id is required'); END;
CREATE TRIGGER integration_accounts_user_required BEFORE INSERT ON integration_accounts
  WHEN NEW.user_id IS NULL AND NEW.provider NOT IN ('anthropic','telegram')
BEGIN SELECT RAISE(ABORT, 'integration_accounts.user_id is required for personal providers'); END;
