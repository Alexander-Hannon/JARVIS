-- V5 M1a identity: one org per database, many users with roles. The single `owner` row becomes user `usr_owner`
-- (role owner, same password); its username is the current display name lowercased (e.g. "aj").
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  email TEXT,
  password_hash TEXT,                 -- NULL until an invited user sets one (and for SSO-only users, M1b)
  role TEXT NOT NULL CHECK (role IN ('owner','admin','approver','member','auditor')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  created_at TEXT NOT NULL,
  last_login_at TEXT,
  disabled_at TEXT
);
INSERT INTO users (id, username, display_name, password_hash, role, created_at, last_login_at)
  SELECT 'usr_owner',
         COALESCE(NULLIF(lower(replace((SELECT display_name FROM user_settings WHERE id=1), ' ', '')), ''), 'owner'),
         COALESCE((SELECT display_name FROM user_settings WHERE id=1), 'Owner'),
         password_hash, 'owner', created_at, created_at
  FROM owner WHERE id=1;
DROP TABLE owner;

-- Org-wide configuration (brand, models, budget limits, MFA policy, research/sandbox policy). Personal preferences
-- stay in user_settings.
CREATE TABLE org_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  data TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
INSERT INTO org_settings (id, data, updated_at)
  SELECT 1,
         json_object(
           'budget', json(COALESCE(json_extract(data, '$.budget'), '{}')),
           'models', json(COALESCE(json_extract(data, '$.models'), '{}')),
           'brand', json(COALESCE(json_extract(data, '$.brand'), '{}'))),
         updated_at
  FROM user_settings WHERE id=1;
INSERT OR IGNORE INTO org_settings (id, data, updated_at) VALUES (1, '{}', strftime('%Y-%m-%dT%H:%M:%fZ','now'));

-- Second factors. TOTP secrets are sealed with the app key; WebAuthn stores the public key only.
CREATE TABLE user_mfa (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('totp','webauthn')),
  label TEXT NOT NULL,
  secret_sealed TEXT,                 -- totp
  credential_id TEXT UNIQUE,          -- webauthn (base64url)
  public_key TEXT,                    -- webauthn (base64url COSE key)
  sign_count INTEGER NOT NULL DEFAULT 0,
  confirmed_at TEXT,                  -- totp counts only after one valid code
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  last_step INTEGER                   -- last accepted TOTP time step (no replay within the window)
);
CREATE INDEX user_mfa_user ON user_mfa(user_id);
CREATE TABLE mfa_recovery_codes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  used_at TEXT
);
-- A password check that still needs a second factor (5 minutes), or a pending WebAuthn ceremony.
CREATE TABLE mfa_challenges (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose TEXT NOT NULL CHECK (purpose IN ('login','webauthn_register','webauthn_login')),
  challenge TEXT,
  label TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE TABLE invites (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
-- Messaging channels (Telegram) map a chat to exactly one user.
CREATE TABLE channel_links (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  channel TEXT NOT NULL CHECK (channel IN ('telegram')),
  chat_id TEXT NOT NULL,
  chat_name TEXT,
  conversation_id TEXT,
  paired_at TEXT NOT NULL,
  UNIQUE (channel, chat_id),
  UNIQUE (channel, user_id)
);

-- Sessions, pairing codes and OAuth states belong to a user. `auth_level` says whether a second factor was used.
ALTER TABLE sessions ADD COLUMN user_id TEXT REFERENCES users(id);
ALTER TABLE sessions ADD COLUMN auth_level TEXT NOT NULL DEFAULT 'password';
UPDATE sessions SET user_id='usr_owner';
-- Sessions from before users existed can't be attributed reliably on a fresh DB; on AJ's DB they're his.
DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users);
ALTER TABLE pairing_codes ADD COLUMN user_id TEXT REFERENCES users(id);
UPDATE pairing_codes SET user_id='usr_owner' WHERE EXISTS (SELECT 1 FROM users WHERE id='usr_owner');
ALTER TABLE oauth_states ADD COLUMN user_id TEXT REFERENCES users(id);
-- auth_attempts gains invite/mfa kinds and the username tried (per-account throttles). Rebuilt for the CHECK.
CREATE TABLE auth_attempts_v (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('login','pair','setup','invite','mfa')),
  ip TEXT NOT NULL,
  ok INTEGER NOT NULL,
  username TEXT
);
INSERT INTO auth_attempts_v (id, at, kind, ip, ok) SELECT id, at, kind, ip, ok FROM auth_attempts;
DROP TABLE auth_attempts;
ALTER TABLE auth_attempts_v RENAME TO auth_attempts;
CREATE INDEX auth_attempts_kind_at ON auth_attempts(kind, at);
CREATE INDEX auth_attempts_ip ON auth_attempts(ip, kind, at);
CREATE INDEX auth_attempts_user ON auth_attempts(username, kind, at);
ALTER TABLE audit_events ADD COLUMN actor_user_id TEXT;
UPDATE audit_events SET actor_user_id='usr_owner'
  WHERE actor='user' AND EXISTS (SELECT 1 FROM users WHERE id='usr_owner');
ALTER TABLE approvals ADD COLUMN approved_by TEXT REFERENCES users(id);
UPDATE approvals SET approved_by='usr_owner'
  WHERE channel <> 'policy' AND EXISTS (SELECT 1 FROM users WHERE id='usr_owner');
