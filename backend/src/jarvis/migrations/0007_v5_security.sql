-- V5 M0 security baseline: console setup tokens, persistent login/pairing throttles, and PKCE verifiers sealed at rest.
-- Setup tokens are printed once on the console of the machine running Jarvis (`jarvis serve` / `jarvis setup-token`);
-- only their hash is stored. They let first-run setup happen through an HTTPS front without trusting "loopback".
CREATE TABLE setup_tokens (
  token_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
-- One row per login / pairing / setup attempt. Throttles are computed from this table so they survive restarts
-- and are scoped per client IP (an attacker's failures never lock out a device that has signed in before).
CREATE TABLE auth_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('login','pair','setup')),
  ip TEXT NOT NULL,
  ok INTEGER NOT NULL
);
CREATE INDEX auth_attempts_kind_at ON auth_attempts(kind, at);
CREATE INDEX auth_attempts_ip ON auth_attempts(ip, kind, at);
ALTER TABLE sessions ADD COLUMN ip TEXT;
ALTER TABLE sessions ADD COLUMN user_agent TEXT;
-- Verifiers are now stored encrypted. Pending OAuth states live at most 10 minutes; dropping them only means an
-- in-flight connect started before this upgrade has to be clicked again.
DELETE FROM oauth_states;
