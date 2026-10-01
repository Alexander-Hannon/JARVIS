// Typed fetch wrapper. CSRF token lives in module memory only (never storage).
import type * as T from './types';
import { AUDIO_ERROR_CODES, type AudioErrorCode } from './types';

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null | undefined): void {
  csrfToken = token ?? null;
}
export function hasCsrfToken(): boolean {
  return csrfToken !== null;
}
export function onUnauthorized(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly body: unknown;
  constructor(status: number, message: string, code: string | null, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }
  get isAudioError(): boolean {
    return this.status === 422 && this.code !== null && (AUDIO_ERROR_CODES as readonly string[]).includes(this.code);
  }
  get audioCode(): AudioErrorCode | null {
    return this.isAudioError ? (this.code as AudioErrorCode) : null;
  }
  get isNetwork(): boolean {
    return this.status === 0;
  }
}

function normalizeError(status: number, body: unknown, fallback: string): ApiError {
  let message = fallback;
  let code: string | null = null;
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') message = d;
    else if (d && typeof d === 'object') {
      const o = d as { code?: unknown; message?: unknown };
      if (typeof o.code === 'string') code = o.code;
      if (typeof o.message === 'string') message = o.message;
    } else if (Array.isArray(d)) {
      message = d.map((x) => (x && typeof x === 'object' && 'msg' in x ? String((x as { msg: unknown }).msg) : String(x))).join('; ');
    }
  }
  return new ApiError(status, message, code, body);
}

type Json = Record<string, unknown> | unknown[] | null;

export interface RequestOpts {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  json?: Json | object;
  form?: FormData;
  signal?: AbortSignal;
  /** Don't trigger the global 401 → login handler (used by auth calls). */
  noAuthRedirect?: boolean;
}

export async function request<R>(path: string, opts: RequestOpts = {}): Promise<R> {
  const method = opts.method ?? (opts.json !== undefined || opts.form ? 'POST' : 'GET');
  const headers: Record<string, string> = { Accept: 'application/json' };
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.json);
  }
  if (method !== 'GET' && csrfToken) headers['X-CSRF-Token'] = csrfToken;

  let res: Response;
  try {
    res = await fetch(path, { method, headers, body, credentials: 'same-origin', signal: opts.signal, cache: 'no-store' });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'Cannot reach the JARVIS server. Check that the backend is running.', 'network', null);
  }
  const text = await res.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) {
    if (res.status === 401 && !opts.noAuthRedirect && unauthorizedHandler) unauthorizedHandler();
    throw normalizeError(res.status, parsed, `${res.status} ${res.statusText || 'Request failed'}`);
  }
  return parsed as R;
}

/** Local neural TTS (Piper on the Jarvis host). Returns WAV bytes. */
export async function fetchTtsAudio(text: string, preset: string, signal?: AbortSignal, rate = 1): Promise<Blob> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const res = await fetch('/api/voice/tts', {
    method: 'POST', headers, body: JSON.stringify({ text, preset, rate }), credentials: 'same-origin', signal, cache: 'no-store',
  });
  if (!res.ok) throw new ApiError(res.status, `Voice synthesis failed (${res.status})`, 'tts_unavailable', null);
  return res.blob();
}

const q = (params: Record<string, string | number | undefined | null>): string => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') s.set(k, String(v));
  const str = s.toString();
  return str ? `?${str}` : '';
};

export const api = {
  // Auth
  authStatus: () => request<T.AuthStatus>('/api/auth/status', { noAuthRedirect: true }),
  setup: (password: string) => request<T.CsrfResponse>('/api/auth/setup', { json: { password }, noAuthRedirect: true }),
  login: (password: string) => request<T.CsrfResponse>('/api/auth/login', { json: { password }, noAuthRedirect: true }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  pairingCode: () => request<T.PairingCode>('/api/auth/pairing-code', { method: 'POST' }),
  pair: (code: string, label: string) => request<T.CsrfResponse>('/api/auth/pair', { json: { code, label }, noAuthRedirect: true }),

  // Conversation / voice
  createConversation: () => request<{ id: string }>('/api/conversations', { method: 'POST' }),
  messages: (id: string) => request<T.Message[]>(`/api/conversations/${encodeURIComponent(id)}/messages`),
  voiceStatus: () => request<T.VoiceStatus>('/api/voice/status'),
  voiceWarmup: () => request<T.VoiceStatus>('/api/voice/warmup', { method: 'POST' }),
  startVoiceSession: (mode: 'push_to_talk' | 'hands_free', conversation_id: string) =>
    request<T.VoiceSession>('/api/voice/sessions', { json: { mode, conversation_id } }),
  endVoiceSession: (id: string) => request<{ ok: boolean }>(`/api/voice/sessions/${encodeURIComponent(id)}/end`, { method: 'POST' }),
  uploadAudio: (form: FormData, signal?: AbortSignal) => request<T.Capture>('/api/captures/audio', { form, signal }),
  editTranscript: (id: string, text: string) => request<T.Capture>(`/api/captures/${encodeURIComponent(id)}/transcript`, { json: { text } }),
  command: (req: T.CommandRequest) => request<T.CommandResponse>('/api/commands', { json: req }),
  cancelRun: (id: string) => request<{ ok: boolean; status: string }>(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  runEvents: (id: string) => request<T.RunEvent[]>(`/api/runs/${encodeURIComponent(id)}/events`),

  // Proposals
  proposals: (status: 'active' | 'all' = 'active') => request<T.Proposal[]>(`/api/proposals${q({ status })}`),
  proposal: (id: string) => request<T.Proposal>(`/api/proposals/${encodeURIComponent(id)}`),
  approve: (id: string, version: number, hash: string, selected_items: number[]) =>
    request<T.ApproveResponse>(`/api/proposals/${encodeURIComponent(id)}/approve`, { json: { version, hash, selected_items } }),
  reject: (id: string) => request<T.Proposal>(`/api/proposals/${encodeURIComponent(id)}/reject`, { method: 'POST' }),
  revise: (id: string, items: T.ReviseItem[]) => request<T.Proposal>(`/api/proposals/${encodeURIComponent(id)}/revise`, { json: { items } }),
  retry: (id: string, items: number[]) => request<T.Proposal>(`/api/proposals/${encodeURIComponent(id)}/retry`, { json: { items } }),
  actionHistory: (limit = 50) => request<T.ActionHistoryEntry[]>(`/api/actions/history${q({ limit })}`),
  control: () => request<T.ControlState>('/api/control'),
  pause: (reason?: string) => request<T.ControlState>('/api/control/pause', { json: reason ? { reason } : {} }),
  resume: () => request<T.ControlState>('/api/control/resume', { method: 'POST' }),

  // Today / plans / briefings
  today: () => request<T.Today>('/api/today'),
  putPlan: (plan: { mission: string; priorities: string[]; constraints?: string[]; first_action?: string | null }) =>
    request<T.Plan>('/api/plan/today', { method: 'PUT', json: plan }),
  planHistory: (date?: string) => request<T.Plan[]>(`/api/plan/history${q({ date })}`),
  briefings: (kind?: string, limit = 20) => request<T.Briefing[]>(`/api/briefings${q({ kind, limit })}`),
  generateBriefing: (kind: T.BriefingKind, force = false) => request<T.Briefing>('/api/briefings/generate', { json: { kind, force } }),
  nightlyDebrief: (body: { briefing_id?: string; answers: { completed: string[]; changes: string; carryover: string[]; notes: string } }) =>
    request<unknown>('/api/debriefs/nightly', { json: body }),
  schedules: () => request<T.Schedule[]>('/api/schedules'),
  patchSchedule: (id: string, patch: Partial<Pick<T.Schedule, 'enabled' | 'hour' | 'minute' | 'weekdays' | 'destination'>>) =>
    request<T.Schedule>(`/api/schedules/${encodeURIComponent(id)}`, { method: 'PATCH', json: patch }),
  workerStatus: () => request<T.WorkerStatus>('/api/worker/status'),
  notifications: () => request<T.Notification[]>('/api/notifications'),
  markNotificationRead: (id: string) => request<{ ok: boolean }>(`/api/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' }),
  markNotificationOpened: (id: string) => request<{ ok: boolean }>(`/api/notifications/${encodeURIComponent(id)}/opened`, { method: 'POST' }),
  pushConfig: () => request<T.PushConfig>('/api/push/config'),
  pushSubscribe: (sub: { endpoint: string; keys: { p256dh: string; auth: string } }) => request<{ id: string }>('/api/push/subscribe', { json: sub }),
  pushUnsubscribe: (endpoint: string) => request<unknown>('/api/push/subscribe', { method: 'DELETE', json: { endpoint } }),

  // Memory / knowledge
  memories: (p: { q?: string; status?: string; project_id?: string }) => request<T.Memory[]>(`/api/memories${q(p)}`),
  addMemory: (m: { content: string; type: string; project_id?: string; pinned?: boolean; entity?: string }) => request<T.Memory>('/api/memories', { json: m }),
  correctMemory: (id: string, content: string) => request<T.Memory>(`/api/memories/${encodeURIComponent(id)}`, { method: 'PATCH', json: { content } }),
  forgetMemory: (id: string) => request<{ ok: boolean; note: string }>(`/api/memories/${encodeURIComponent(id)}/forget`, { method: 'POST' }),
  pinMemory: (id: string, pinned: boolean) => request<T.Memory>(`/api/memories/${encodeURIComponent(id)}/pin`, { json: { pinned } }),
  projects: () => request<T.Project[]>('/api/projects'),
  patchProject: (id: string, p: { description?: string; notion_url?: string }) => request<T.Project>(`/api/projects/${encodeURIComponent(id)}`, { method: 'PATCH', json: p }),
  uploadDocument: (form: FormData) => request<T.JDocument>('/api/documents', { form }),
  documents: () => request<T.JDocument[]>('/api/documents'),
  document: (id: string) => request<T.JDocument & { segments: T.DocumentSegment[] }>(`/api/documents/${encodeURIComponent(id)}`),
  deleteDocument: (id: string) => request<{ ok: boolean }>(`/api/documents/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  search: (query: string, project_id?: string) => request<T.SearchHit[]>(`/api/search${q({ q: query, project_id })}`),
  evidencePreview: (document_ids: string[], question: string) => request<T.EvidencePreview>('/api/evidence/preview', { json: { document_ids, question } }),

  // Integrations
  integrations: () => request<T.Integration[]>('/api/integrations'),
  todoistConnect: (token: string) => request<T.Integration>('/api/integrations/todoist', { json: { token } }),
  todoistProjects: () => request<{ id: string; name: string }[]>('/api/integrations/todoist/projects'),
  todoistTarget: (project_id: string) => request<T.Integration>('/api/integrations/todoist/target', { json: { project_id } }),
  todoistSync: () => request<{ pulled: number; reconciled: number }>('/api/integrations/todoist/sync', { method: 'POST' }),
  googleSetup: () => request<T.GoogleSetup>('/api/integrations/google/setup'),
  googleStart: (purpose: 'calendar' | 'drive' | 'gmail') =>
    request<{ auth_url: string }>('/api/integrations/google/start', { json: { purpose } }),
  microsoftSetup: () => request<T.MicrosoftSetup>('/api/integrations/microsoft/setup'),
  microsoftStart: (with_send = false) =>
    request<{ auth_url: string }>('/api/integrations/microsoft/start', { json: { with_send } }),
  setMailSending: (mailbox: T.Mailbox, enabled: boolean) =>
    request<T.Integration>('/api/integrations/mail/sending', { json: { mailbox, enabled } }),
  inbox: (params: { unread_only?: boolean; limit?: number; q?: string } = {}) => {
    const qs = new URLSearchParams();
    if (params.unread_only !== undefined) qs.set('unread_only', String(params.unread_only));
    if (params.limit) qs.set('limit', String(params.limit));
    if (params.q) qs.set('q', params.q);
    return request<T.InboxResponse>(`/api/inbox?${qs.toString()}`);
  },
  inboxThread: (ref: string) => request<T.MailThread>(`/api/inbox/thread?ref=${encodeURIComponent(ref)}`),
  googleCalendars: () => request<T.GoogleCalendar[]>('/api/integrations/google/calendars'),
  setGoogleCalendars: (read: string[], write_target: string | null) => request<unknown>('/api/integrations/google/calendars', { json: { read, write_target } }),
  driveRoots: () => request<unknown>('/api/integrations/drive/roots'),
  setDriveRoots: (ids: string[]) => request<unknown>('/api/integrations/drive/roots', { json: { ids } }),
  icsConnect: (url: string) => request<T.Integration>('/api/integrations/ics', { json: { url } }),
  icsRefresh: () => request<{ items: number; changed: number; last_refresh: string }>('/api/integrations/ics/refresh', { method: 'POST' }),
  deadlines: () => request<T.Deadline[]>('/api/deadlines'),
  courseMappings: () => request<T.CourseMapping[]>('/api/course-mappings'),
  addCourseMapping: (m: T.CourseMapping) => request<unknown>('/api/course-mappings', { json: m }),
  githubConnect: (token: string, repos: string[]) => request<unknown>('/api/integrations/github', { json: { token, repos } }),
  githubStatus: () => request<unknown>('/api/github/status'),
  anthropicKey: (api_key: string) => request<unknown>('/api/integrations/anthropic', { json: { api_key } }),
  disconnect: (account_id: string) => request<unknown>(`/api/integrations/${encodeURIComponent(account_id)}`, { method: 'DELETE' }),
  localRoots: () => request<T.LocalRoot[]>('/api/localops/roots'),
  addLocalRoot: (r: { path: string; label: string; writable: boolean }) => request<T.LocalRoot>('/api/localops/roots', { json: r }),
  vault: () => request<T.VaultStatus>('/api/vault'),
  setVaultIndexed: (rootId: string, enabled: boolean) =>
    request<T.VaultStatus>(`/api/vault/roots/${encodeURIComponent(rootId)}`, { json: { enabled } }),
  reindexVault: (rootId: string) => request<{ queued: boolean }>(`/api/vault/roots/${encodeURIComponent(rootId)}/reindex`, { method: 'POST' }),
  removeLocalRoot: (id: string) => request<unknown>(`/api/localops/roots/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  settings: () => request<T.Settings>('/api/settings'),
  patchSettings: (p: Partial<T.Settings>) => request<T.Settings>('/api/settings', { method: 'PATCH', json: p }),
  budget: () => request<T.Budget>('/api/budget'),
  weather: () => request<import('../components/Visuals').WeatherVisual>('/api/weather'),
  brain: () =>
    request<{ mode: 'v2_agent' | 'offline'; reason: string | null; web_research: boolean; max_searches: number; model: string; tools: string[]; notes: string }>(
      '/api/brain',
    ),
  setResearch: (research: { web_enabled?: boolean; max_searches?: number }) =>
    request<unknown>('/api/settings', { method: 'PATCH', json: { research } }),
  patchBudget: (b: Partial<T.Budget>) => request<T.Budget>('/api/budget', { method: 'PATCH', json: b }),
  doctor: () => request<{ checks: T.DoctorCheck[] }>('/api/doctor'),
  latency: () => request<T.LatencyMetrics>('/api/metrics/latency'),
  activity: (limit = 100) => request<T.ActivityEvent[]>(`/api/activity${q({ limit })}`),
  createBackup: () => request<T.Backup>('/api/backups', { method: 'POST' }),
  backups: () => request<T.Backup[]>('/api/backups'),
  restorePreview: (id: string) => request<T.RestorePreview>(`/api/backups/${encodeURIComponent(id)}/restore-preview`, { method: 'POST' }),
  restore: (id: string) => request<{ ok: boolean; pre_restore_backup_id: string }>(`/api/backups/${encodeURIComponent(id)}/restore`, { json: { confirm: true } }),
};

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

/**
 * Multipart upload via XHR so the UI can truthfully switch from "uploading" to
 * "transcribing" when the request body has been fully sent.
 */
export function uploadWithProgress<R>(
  path: string,
  form: FormData,
  hooks: { onUploaded?: () => void; onProgress?: (fraction: number) => void; signal?: AbortSignal } = {},
): Promise<R> {
  return new Promise<R>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'application/json');
    if (csrfToken) xhr.setRequestHeader('X-CSRF-Token', csrfToken);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable && hooks.onProgress) hooks.onProgress(ev.loaded / ev.total);
    };
    xhr.upload.onload = () => hooks.onUploaded?.();
    xhr.onerror = () => reject(new ApiError(0, 'Upload failed: cannot reach the JARVIS server.', 'network', null));
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    xhr.onload = () => {
      let parsed: unknown = null;
      if (xhr.responseText) {
        try {
          parsed = JSON.parse(xhr.responseText);
        } catch {
          parsed = xhr.responseText;
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(parsed as R);
      else {
        if (xhr.status === 401 && unauthorizedHandler) unauthorizedHandler();
        reject(normalizeError(xhr.status, parsed, `${xhr.status} ${xhr.statusText || 'Upload failed'}`));
      }
    };
    hooks.signal?.addEventListener('abort', () => xhr.abort());
    xhr.send(form);
  });
}
