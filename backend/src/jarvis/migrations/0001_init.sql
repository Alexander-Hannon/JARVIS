-- JARVIS initial schema. All instants are ISO-8601 UTC text ("...Z").
-- Local wall-clock semantics are stored alongside as (local text, IANA tz).

CREATE TABLE user_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  display_name TEXT NOT NULL DEFAULT 'AJ',
  timezone TEXT NOT NULL DEFAULT 'America/Chicago',
  data TEXT NOT NULL DEFAULT '{}',          -- JSON: voice, privacy, budget, notification prefs
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE control_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  actions_paused INTEGER NOT NULL DEFAULT 0,
  paused_at TEXT,
  paused_reason TEXT,
  resumed_at TEXT
);

-- ---------- auth ----------
CREATE TABLE owner (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  csrf_token TEXT NOT NULL,
  label TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at TEXT
);
CREATE TABLE pairing_codes (
  id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

-- ---------- integrations ----------
CREATE TABLE integration_accounts (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,                  -- todoist | google | github | ics | anthropic
  label TEXT NOT NULL,
  external_account_id TEXT,                -- e.g. google email / todoist user id
  status TEXT NOT NULL DEFAULT 'disconnected', -- connected|disconnected|error|revoked|expired
  secret_ciphertext BLOB,                  -- Fernet-encrypted token/URL; key lives outside DB
  secret_kind TEXT,
  scopes TEXT NOT NULL DEFAULT '[]',
  config TEXT NOT NULL DEFAULT '{}',
  last_sync_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (provider, external_account_id)
);
CREATE TABLE scope_grants (
  id TEXT PRIMARY KEY,
  account_id TEXT REFERENCES integration_accounts(id) ON DELETE CASCADE,
  resource_type TEXT NOT NULL,   -- calendar | todoist_project | drive_file | drive_folder | github_repo | local_root | markdown_root
  resource_id TEXT NOT NULL,
  resource_name TEXT,
  access TEXT NOT NULL CHECK (access IN ('read','write')),
  is_default_target INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (account_id, resource_type, resource_id, access)
);
CREATE TABLE oauth_states (
  state_hash TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  purpose TEXT NOT NULL,
  code_verifier TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

-- ---------- conversation / capture ----------
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  title TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE voice_sessions (
  id TEXT PRIMARY KEY,
  conversation_id TEXT REFERENCES conversations(id),
  mode TEXT NOT NULL CHECK (mode IN ('push_to_talk','hands_free')),
  status TEXT NOT NULL DEFAULT 'active',
  started_at TEXT NOT NULL,
  max_duration_s INTEGER NOT NULL DEFAULT 600,
  ended_at TEXT
);
CREATE TABLE captures (
  id TEXT PRIMARY KEY,
  conversation_id TEXT REFERENCES conversations(id),
  voice_session_id TEXT REFERENCES voice_sessions(id),
  kind TEXT NOT NULL CHECK (kind IN ('audio','text','attachment')),
  captured_at TEXT NOT NULL,       -- UTC instant of the user's utterance (reference time)
  capture_tz TEXT NOT NULL,        -- IANA tz used for relative date resolution
  status TEXT NOT NULL,            -- received|transcribing|transcribed|failed|cancelled
  audio_mime TEXT,
  audio_bytes INTEGER,
  audio_duration_s REAL,
  audio_path TEXT,                 -- only set when retention opt-in
  audio_retained INTEGER NOT NULL DEFAULT 0,
  audio_deleted_at TEXT,
  error_code TEXT,
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE transcript_versions (
  id TEXT PRIMARY KEY,
  capture_id TEXT NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('stt','user_edit','typed')),
  stt_engine TEXT,
  stt_model TEXT,
  confidence REAL,                 -- 0..1 derived from avg logprob; NULL for typed
  latency_ms INTEGER,
  created_at TEXT NOT NULL,
  UNIQUE (capture_id, version)
);
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user','assistant')),
  text TEXT NOT NULL,
  spoken_text TEXT,
  capture_id TEXT REFERENCES captures(id),
  run_id TEXT,
  proposal_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  conversation_id TEXT REFERENCES conversations(id),
  capture_id TEXT REFERENCES captures(id),
  kind TEXT NOT NULL,               -- command | briefing | ingest | reconcile
  status TEXT NOT NULL,             -- running|completed|failed|cancelled|needs_clarification
  intent TEXT,                      -- JSON
  specialists TEXT NOT NULL DEFAULT '[]',
  limits TEXT NOT NULL DEFAULT '{}',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,               -- route|tool|status|error|approval|verify
  message TEXT NOT NULL,            -- concise user-facing text, no chain-of-thought
  data TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- ---------- projects / knowledge ----------
CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  area TEXT NOT NULL,               -- school | gta | business | coaching | faith | training | ai_guru | admin
  description TEXT NOT NULL DEFAULT '',
  notion_url TEXT,
  sensitive INTEGER NOT NULL DEFAULT 0,  -- student/client material; never attached to unrelated runs
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE source_documents (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  origin TEXT NOT NULL,             -- upload | drive | markdown | local | meeting
  origin_ref TEXT,                  -- drive file id / relative path
  title TEXT NOT NULL,
  mime TEXT,
  current_version_id TEXT,
  created_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE TABLE source_versions (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  stored_path TEXT,
  remote_modified_at TEXT,
  fetched_at TEXT NOT NULL,
  status TEXT NOT NULL,             -- processed | partial | failed
  processing TEXT NOT NULL DEFAULT '{}',  -- JSON per-stage status, visual coverage
  UNIQUE (document_id, version)
);
CREATE TABLE evidence_segments (
  id TEXT PRIMARY KEY,
  source_version_id TEXT NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
  locator TEXT NOT NULL,            -- "page 3", "section 2", "cell 5", "image 1 (page 2)"
  kind TEXT NOT NULL CHECK (kind IN ('text','image','page_image')),
  text TEXT NOT NULL DEFAULT '',
  image_path TEXT,
  image_mime TEXT,
  created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE evidence_fts USING fts5(text, segment_id UNINDEXED, tokenize='porter unicode61');

CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  entity TEXT,
  type TEXT NOT NULL CHECK (type IN ('preference','fact','decision','observation','hypothesis','context','goal','constraint')),
  status TEXT NOT NULL CHECK (status IN ('active','superseded','forgotten','conflict','expired','hypothesis')),
  pinned INTEGER NOT NULL DEFAULT 0,
  confirmed_by_user INTEGER NOT NULL DEFAULT 0,
  scope TEXT NOT NULL DEFAULT 'global',  -- global | project
  current_version INTEGER NOT NULL DEFAULT 1,
  superseded_by TEXT REFERENCES memories(id),
  conflict_with TEXT REFERENCES memories(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE memory_versions (
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  source_kind TEXT NOT NULL,        -- user_statement | user_edit | seed | document | agent_suggestion
  source_ref TEXT,
  observed_at TEXT NOT NULL,
  effective_from TEXT,
  review_at TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (memory_id, version)
);
CREATE VIRTUAL TABLE memory_fts USING fts5(content, memory_id UNINDEXED, tokenize='porter unicode61');

CREATE TABLE daily_plans (
  id TEXT PRIMARY KEY,
  plan_date TEXT NOT NULL UNIQUE,    -- local date in user tz
  current_version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE daily_plan_versions (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES daily_plans(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  mission TEXT NOT NULL,
  priorities TEXT NOT NULL DEFAULT '[]',
  constraints TEXT NOT NULL DEFAULT '[]',
  first_action TEXT,
  author TEXT NOT NULL CHECK (author IN ('user','chief','briefing')),
  status TEXT NOT NULL CHECK (status IN ('proposed','accepted','superseded')),
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (plan_id, version)
);

-- ---------- proposals / actions ----------
CREATE TABLE proposals (
  id TEXT PRIMARY KEY,
  conversation_id TEXT REFERENCES conversations(id),
  run_id TEXT REFERENCES runs(id),
  capture_id TEXT REFERENCES captures(id),
  status TEXT NOT NULL,              -- proposed|approved|rejected|expired|superseded|queued|executing|verified|partial|failed|unknown|cancelled
  current_version INTEGER NOT NULL DEFAULT 1,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  row_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE proposal_versions (
  id TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  hash TEXT NOT NULL,                -- sha256 of canonical action payloads
  summary TEXT NOT NULL,
  reason TEXT,
  assumptions TEXT NOT NULL DEFAULT '[]',
  questions TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT '{}', -- capture/transcript refs
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  superseded_at TEXT,
  presented_at TEXT,                 -- when this exact version was shown/read back to AJ
  UNIQUE (proposal_id, version)
);
CREATE TABLE proposed_actions (
  id TEXT PRIMARY KEY,
  proposal_version_id TEXT NOT NULL REFERENCES proposal_versions(id) ON DELETE CASCADE,
  item_index INTEGER NOT NULL,
  action_type TEXT NOT NULL,         -- task.create|task.update|task.complete|task.delete|event.create|event.update|event.delete|file.write|file.move|memory.* (local)
  provider TEXT NOT NULL,            -- todoist | google_calendar | local_fs
  account_id TEXT REFERENCES integration_accounts(id),
  target_resource TEXT,              -- project id / calendar id / root id
  args TEXT NOT NULL,                -- JSON exact arguments
  timezone TEXT NOT NULL,
  before_state TEXT,
  after_state TEXT,
  preconditions TEXT NOT NULL DEFAULT '{}',
  expected_effect TEXT NOT NULL,
  reason TEXT,
  missing_fields TEXT NOT NULL DEFAULT '[]',
  op_key TEXT NOT NULL UNIQUE,       -- stable operation id; basis for provider idempotency
  state TEXT NOT NULL DEFAULT 'proposed', -- proposed|approved|queued|executing|verified|failed|unknown|cancelled|skipped|conflict|superseded
  external_id TEXT,
  external_url TEXT,
  etag TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (proposal_version_id, item_index)
);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY,
  proposal_version_id TEXT NOT NULL REFERENCES proposal_versions(id),
  proposal_hash TEXT NOT NULL,
  selected_items TEXT NOT NULL,      -- JSON list of item_index
  channel TEXT NOT NULL CHECK (channel IN ('button','voice','typed')),
  session_id TEXT REFERENCES sessions(id),
  evidence TEXT,                     -- transcript text for voice/typed approvals
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE TABLE execution_attempts (
  id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL REFERENCES proposed_actions(id),
  job_id TEXT,
  attempt_no INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,                      -- success|failed|unknown|conflict|precondition_failed|cancelled|blocked
  request_hash TEXT,
  response_summary TEXT,
  error TEXT
);
CREATE TABLE verification_receipts (
  id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL REFERENCES proposed_actions(id),
  verified_at TEXT NOT NULL,
  method TEXT NOT NULL,              -- read_after_write | reconcile_marker | reconcile_id | local_stat
  ok INTEGER NOT NULL,
  provider_ids TEXT NOT NULL DEFAULT '{}',
  observed TEXT NOT NULL DEFAULT '{}',
  simulated INTEGER NOT NULL DEFAULT 0   -- 1 when produced against a contract mock
);
CREATE TABLE task_links (
  id TEXT PRIMARY KEY,
  op_key TEXT UNIQUE,
  action_id TEXT REFERENCES proposed_actions(id),
  account_id TEXT REFERENCES integration_accounts(id),
  external_id TEXT,
  url TEXT,
  content TEXT NOT NULL,
  project_ext_id TEXT,
  due TEXT,                          -- JSON
  status TEXT NOT NULL,              -- pending_sync | active | completed | deleted | unknown
  remote_updated_at TEXT,
  last_synced_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE calendar_links (
  id TEXT PRIMARY KEY,
  op_key TEXT UNIQUE,
  action_id TEXT REFERENCES proposed_actions(id),
  account_id TEXT REFERENCES integration_accounts(id),
  calendar_id TEXT NOT NULL,
  event_id TEXT,
  etag TEXT,
  html_link TEXT,
  summary TEXT NOT NULL,
  start_at TEXT,
  end_at TEXT,
  tz TEXT,
  task_link_id TEXT REFERENCES task_links(id),
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ---------- jobs / schedules ----------
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('queued','leased','done','failed','cancelled','dead','paused')),
  dedupe_key TEXT UNIQUE,
  run_after TEXT NOT NULL,
  lease_owner TEXT,
  lease_expires_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_error TEXT,
  result TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX jobs_ready ON jobs(status, run_after);

CREATE TABLE schedules (
  id TEXT PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
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
  row_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE schedule_runs (
  id TEXT PRIMARY KEY,
  schedule_id TEXT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  scheduled_for TEXT NOT NULL,      -- UTC instant
  local_wall TEXT NOT NULL,         -- local date+time intent, e.g. 2026-11-01T06:30 America/Chicago
  dedupe_key TEXT NOT NULL UNIQUE,  -- schedule key + local date: prevents DST fall-back duplicates
  status TEXT NOT NULL,             -- queued | completed | missed | failed
  catch_up INTEGER NOT NULL DEFAULT 0,
  job_id TEXT REFERENCES jobs(id),
  created_at TEXT NOT NULL
);
CREATE TABLE worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  last_beat_at TEXT NOT NULL,
  pid INTEGER,
  host TEXT
);

-- ---------- briefings / notifications ----------
CREATE TABLE briefings (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('daily_command','nightly_debrief','weekly_war_room')),
  for_date TEXT NOT NULL,
  plan_version_id TEXT REFERENCES daily_plan_versions(id),
  input_hash TEXT NOT NULL,
  content TEXT NOT NULL,            -- JSON card
  spoken_summary TEXT NOT NULL,
  sources TEXT NOT NULL,            -- JSON per-source status (ok/stale/unavailable)
  generated_by TEXT NOT NULL,       -- deterministic | model:<id>
  schedule_run_id TEXT REFERENCES schedule_runs(id),
  catch_up INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE debriefs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('nightly','meeting')),
  briefing_id TEXT REFERENCES briefings(id),
  source_document_id TEXT REFERENCES source_documents(id),
  content TEXT NOT NULL,            -- JSON: decisions, commitments, open owners, drafts, answers
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE notifications (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  ref_type TEXT,
  ref_id TEXT,
  created_at TEXT NOT NULL,
  read_at TEXT,
  push_status TEXT NOT NULL DEFAULT 'not_requested', -- not_requested|queued|accepted|failed|no_subscription
  push_accepted_at TEXT,
  opened_at TEXT
);
CREATE TABLE push_subscriptions (
  id TEXT PRIMARY KEY,
  endpoint TEXT NOT NULL UNIQUE,
  keys TEXT NOT NULL,
  user_agent TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  last_error TEXT
);

-- ---------- deadlines (ICS) ----------
CREATE TABLE course_mappings (
  id TEXT PRIMARY KEY,
  pattern TEXT NOT NULL UNIQUE,     -- case-insensitive substring, e.g. "IS 640"
  course TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('own','gta')),
  confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE deadline_items (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES integration_accounts(id) ON DELETE CASCADE,
  uid TEXT NOT NULL,
  recurrence_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL,
  description TEXT,
  url TEXT,
  start_at TEXT,
  end_at TEXT,
  all_day INTEGER NOT NULL DEFAULT 0,
  local_date TEXT,
  tz TEXT,
  status TEXT NOT NULL,             -- confirmed|tentative|cancelled
  sequence INTEGER NOT NULL DEFAULT 0,
  course TEXT,
  role TEXT,                        -- own | gta | unknown
  also_in_calendar INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE (account_id, uid, recurrence_id)
);

-- ---------- local operator ----------
CREATE TABLE file_backups (
  id TEXT PRIMARY KEY,
  root_id TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  action_id TEXT REFERENCES proposed_actions(id),
  created_at TEXT NOT NULL
);

-- ---------- audit / usage / metrics ----------
CREATE TABLE audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('user','system','model','worker')),
  kind TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  summary TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_at ON audit_events(at);
CREATE TABLE usage_reservations (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  purpose TEXT NOT NULL,
  model TEXT NOT NULL,
  est_cost_usd REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('reserved','reconciled','released')),
  scheduled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE usage_records (
  id TEXT PRIMARY KEY,
  reservation_id TEXT REFERENCES usage_reservations(id),
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL,
  measured INTEGER NOT NULL,        -- 1 = from provider usage fields, 0 = estimate
  request_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE latency_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,               -- audio_upload | transcription | first_response | proposal | execution | verification | stt_warmup
  ms INTEGER NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL
);
