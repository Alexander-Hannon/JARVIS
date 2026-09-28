// Types mirroring docs/API_CONTRACT.md (v1). Keep in sync with the contract.
// Fields the contract leaves loosely specified are typed as `unknown` or
// optional so the UI renders defensively instead of assuming shapes.

export type ISODate = string;

// ---------- Auth ----------
export interface AuthStatus {
  owner_exists: boolean;
  authenticated: boolean;
  csrf_token?: string | null;
  remote_mode: boolean;
  setup_allowed: boolean;
}
export interface CsrfResponse {
  csrf_token: string;
}
export interface PairingCode {
  code: string;
  expires_at: ISODate;
}

// ---------- Conversation / voice ----------
export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system' | string;
  text: string;
  spoken_text: string | null;
  capture_id: string | null;
  proposal_id: string | null;
  created_at: ISODate;
}

export interface VoiceStatus {
  engine: string | null;
  model: string | null;
  device: string | null;
  state: string;
  downloaded: boolean;
  download_size_mb: number | null;
  load_ms: number | null;
  warmup_ms: number | null;
  error: string | null;
  local_only: boolean;
  hardware: unknown;
  last_latency_ms: number | null;
}

export interface VoiceSession {
  id: string;
  max_duration_s: number;
}

export interface CaptureTranscript {
  version: number;
  text: string;
  source: string;
  confidence: number | null;
  latency_ms: number | null;
  model: string | null;
}
export interface Capture {
  capture_id: string;
  status: string;
  audio: { mime: string; bytes: number; duration_s: number | null; format: string | null };
  transcript: CaptureTranscript | null;
  error: { code: string; message: string } | null;
}

export const AUDIO_ERROR_CODES = [
  'stt_unavailable',
  'decode_failed',
  'too_large',
  'too_long',
  'empty_audio',
  'unsupported_format',
  'no_speech',
] as const;
export type AudioErrorCode = (typeof AUDIO_ERROR_CODES)[number];

export interface CommandRequest {
  conversation_id: string;
  text: string;
  channel: 'voice' | 'typed';
  capture_id?: string;
  transcript_version?: number;
  captured_at: ISODate;
  tz: string;
  speech_playing: boolean;
  attachments?: string[];
}

export interface RunEvent {
  id: string;
  run_id: string;
  kind: string;
  message: string;
  data: unknown;
  created_at: ISODate;
}

export interface Citation {
  document_id: string;
  title: string;
  locator: string;
  excerpt: string;
  segment_id: string | null;
}

export interface CommandResponse {
  run_id: string;
  status: string;
  message: Message;
  proposal: Proposal | null;
  questions: string[];
  citations: Citation[];
  events: RunEvent[];
  intent: string;
  specialists: string[];
}

// ---------- Proposals ----------
export type ProposalStatus =
  | 'proposed'
  | 'approved'
  | 'queued'
  | 'executing'
  | 'verified'
  | 'partial'
  | 'failed'
  | 'unknown'
  | 'rejected'
  | 'expired'
  | 'superseded'
  | 'cancelled';

export type ItemState =
  | 'proposed'
  | 'approved'
  | 'queued'
  | 'executing'
  | 'verified'
  | 'failed'
  | 'unknown'
  | 'conflict'
  | 'cancelled'
  | 'skipped'
  | 'superseded'
  | 'paused';

export interface Receipt {
  verified_at: ISODate | null;
  method: string | null;
  ok: boolean;
  simulated: boolean;
  observed: unknown;
}

export type Args = Record<string, unknown>;

export interface ProposedItem {
  index: number;
  action_type: string;
  provider: string;
  account_label: string | null;
  target: { id: string | null; name: string | null } | null;
  args: Args;
  before: Args | null;
  after: Args | null;
  expected_effect: string | null;
  reason: string | null;
  missing_fields: string[];
  state: ItemState;
  external_id: string | null;
  external_url: string | null;
  last_error: string | null;
  receipt: Receipt | null;
}

export interface Proposal {
  id: string;
  status: ProposalStatus;
  title: string;
  version: number;
  hash: string;
  summary: string;
  assumptions: string[];
  questions: string[];
  expires_at: ISODate | null;
  presented_at: ISODate | null;
  items: ProposedItem[];
  versions: { version: number; hash: string; created_at: ISODate; superseded_at: ISODate | null }[];
  approval: { id: string; selected_items: number[]; channel: string; created_at: ISODate } | null;
}

export interface ApproveResponse {
  approval_id: string;
  proposal: Proposal;
}

export interface ReviseItem {
  index: number;
  args?: Args;
  remove?: boolean;
}

export interface ActionHistoryEntry {
  id?: string;
  action_type?: string;
  provider?: string;
  account_label?: string | null;
  target?: { id: string | null; name: string | null } | null;
  args?: Args;
  before?: Args | null;
  after?: Args | null;
  state?: string;
  external_url?: string | null;
  external_id?: string | null;
  undo?: string | null;
  undoable?: boolean;
  request_text?: string | null;
  proposal_id: string;
  proposal_title: string;
  approved_at: ISODate | null;
  receipt: Receipt | null;
  attempts: { at?: ISODate; state?: string; error?: string | null; [k: string]: unknown }[];
  [k: string]: unknown;
}

export interface ControlState {
  actions_paused: boolean;
  paused_at: ISODate | null;
  reason: string | null;
  in_flight: { action_id: string; summary: string; state: string }[];
}

// ---------- Today / plan / briefings ----------
export type SourceStatus = 'ok' | 'stale' | 'unavailable' | 'not_connected';
export interface SourceInfo {
  status: SourceStatus;
  last_sync: ISODate | null;
  detail: string | null;
}
export interface Plan {
  mission: string;
  priorities: string[];
  constraints: string[];
  first_action: string | null;
  version: number;
  author: string;
  created_at?: ISODate;
  date?: string;
}
export interface AgendaEvent {
  id?: string;
  title?: string;
  summary?: string;
  start?: ISODate;
  end?: ISODate;
  all_day?: boolean;
  calendar?: string;
  location?: string;
  [k: string]: unknown;
}
export interface Task {
  id?: string;
  content?: string;
  title?: string;
  due?: string | { date?: string; datetime?: string } | null;
  priority?: number;
  project?: string;
  url?: string;
  [k: string]: unknown;
}
export interface Deadline {
  id?: string;
  title?: string;
  due_at?: ISODate;
  course?: string;
  source?: string;
  url?: string;
  [k: string]: unknown;
}
export interface Today {
  date: string;
  tz: string;
  now: ISODate;
  plan: Plan | null;
  agenda: AgendaEvent[];
  tasks_due: Task[];
  overdue?: Task[];
  deadlines: Deadline[];
  conflicts: string[];
  work_windows: { start: ISODate; end: ISODate; label: string }[];
  sources: Record<string, SourceInfo>;
}

export type BriefingKind = 'daily_command' | 'nightly_debrief' | 'weekly_war_room';
export interface Briefing {
  id: string;
  kind: BriefingKind;
  for_date: string;
  content: {
    headline?: string;
    mission?: string;
    priorities?: unknown[];
    agenda?: unknown[];
    due?: unknown[];
    deadlines?: unknown[];
    windows?: unknown[];
    conflicts?: unknown[];
    obstacle?: string | null;
    first_action?: string | null;
    sections?: { title: string; items: unknown[] }[];
  };
  spoken_summary: string | null;
  sources: Record<string, SourceInfo | string> | null;
  generated_by: string | null;
  catch_up: boolean;
  created_at: ISODate;
}

export interface Schedule {
  id: string;
  key: string;
  workflow: string;
  label: string;
  tz: string;
  hour: number;
  minute: number;
  weekdays: number[] | string | null;
  enabled: boolean;
  destination: string | null;
  next_run_at: ISODate | null;
  last_run_at: ISODate | null;
  last_status: string | null;
  last_success_at: ISODate | null;
}
export interface WorkerStatus {
  alive: boolean;
  last_beat_at: ISODate | null;
  worker_id: string | null;
  pid: number | null;
}
export interface Notification {
  id: string;
  kind: string;
  title: string;
  body: string;
  ref_type: string | null;
  ref_id: string | null;
  created_at: ISODate;
  read_at: ISODate | null;
  push_status: string | null;
  opened_at: ISODate | null;
}
export interface PushConfig {
  supported: boolean;
  vapid_public_key: string | null;
  reason?: string;
}

// ---------- Memory / knowledge ----------
export type MemoryStatus = 'active' | 'superseded' | 'conflict' | 'forgotten';
export interface Memory {
  id: string;
  type: string;
  status: MemoryStatus;
  content: string;
  version: number;
  pinned: boolean;
  confirmed_by_user: boolean;
  project_id: string | null;
  entity: string | null;
  source_kind: string | null;
  source_ref: string | null;
  observed_at: ISODate | null;
  review_at: ISODate | null;
  expires_at: ISODate | null;
  superseded_by: string | null;
  conflict_with: string | null;
  versions?: { version: number; content: string; created_at?: ISODate }[];
}
export interface Project {
  id: string;
  slug: string;
  name: string;
  area: string;
  description: string | null;
  notion_url: string | null;
  sensitive: boolean;
  next_actions: unknown[];
  memories_count: number;
}
export interface DocumentSegment {
  id: string;
  locator: string;
  kind: string;
  excerpt: string;
}
export interface JDocument {
  id: string;
  title: string;
  mime: string;
  origin: string;
  project_id: string | null;
  created_at: ISODate;
  version: number;
  sha256: string;
  status: string;
  processing: {
    visual?: { images?: number; rendered_pages?: number; needs_vision?: boolean };
    [stage: string]: unknown;
  } | null;
  segments: number | DocumentSegment[] | null;
}
export type SearchHit = Citation & { kind: string; score: number };
export interface EvidencePreview {
  blocks: { type: 'text' | 'image'; locator: string; chars?: number; bytes?: number; media_type?: string }[];
  estimated_input_tokens: number;
  leaves_machine: boolean;
}

// ---------- Integrations / settings ----------
export interface Grant {
  resource_type: string;
  resource_id: string;
  resource_name: string;
  access: string;
  is_default_target: boolean;
}
export interface Integration {
  provider: string;
  account_id: string;
  status: string;
  label: string | null;
  scopes: string[];
  last_sync_at: ISODate | null;
  last_error: string | null;
  grants: Grant[];
  live_verified: boolean;
}
export interface GoogleSetup {
  configured: boolean;
  redirect_uri: string;
  required_apis: string[];
  scopes: { calendar: string[]; drive: string[] };
}
export interface GoogleCalendar {
  id: string;
  summary: string;
  accessRole: string;
  primary: boolean;
  selected_read: boolean;
  write_target: boolean;
}
export interface LocalRoot {
  id: string;
  path: string;
  label: string;
  writable: boolean;
}
export interface Settings {
  display_name?: string | null;
  timezone?: string | null;
  voice?: { voice_name: string | null; rate: number; muted: boolean; auto_speak: boolean };
  privacy?: { retain_audio: boolean };
  [k: string]: unknown;
}
export interface Budget {
  paid_inference_enabled: boolean;
  background_paid_enabled: boolean;
  daily_limit_usd: number;
  monthly_limit_usd: number;
  max_output_tokens: number;
  max_tool_calls: number;
  max_specialists: number;
  effort: string;
  spent_today_usd: number;
  spent_month_usd: number;
  reserved_usd: number;
  pricing: unknown;
  measured_vs_estimated: unknown;
}
export interface DoctorCheck {
  name: string;
  status: 'ok' | 'warn' | 'fail' | 'pending';
  detail: string | null;
  remedy: string | null;
}
export type LatencyMetrics = Record<string, { count: number; p50_ms: number | null; p95_ms: number | null; last_ms: number | null }>;
export interface ActivityEvent {
  at: ISODate;
  kind: string;
  message: string;
  run_id?: string;
  entity_type?: string;
  entity_id?: string;
}
export interface Backup {
  id: string;
  path: string;
  sha256: string;
  bytes: number;
  schema_version: number | string;
  created_at?: ISODate;
}
export interface RestorePreview {
  compatible: boolean;
  counts: Record<string, number>;
  will_disable: string[];
  pre_restore_backup: boolean;
}
export interface CourseMapping {
  id?: string;
  course?: string;
  project_id?: string;
  [k: string]: unknown;
}
