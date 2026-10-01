# JARVIS HTTP API contract (v1)

Base: same origin as the UI, prefix `/api`. JSON unless stated. Times: ISO-8601 UTC (`...Z`) plus
explicit IANA `tz` where local semantics matter. Errors: `{"detail": string | {"code": string, "message": string}}`
with a meaningful HTTP status.

Auth: cookie `jarvis_session` (HttpOnly, SameSite=Strict). Every non-GET request made with the cookie
must send header `X-CSRF-Token: <csrf_token>` (returned by setup/login/status). `401` → show login.

## Auth
| Method | Path | Body → Response |
|---|---|---|
| GET | /api/auth/status | → `{owner_exists, authenticated, csrf_token?, remote_mode, setup_allowed}` |
| POST | /api/auth/setup | `{password}` (loopback only, once) → `{csrf_token}` + cookie |
| POST | /api/auth/login | `{password}` → `{csrf_token}` + cookie |
| POST | /api/auth/logout | → `{ok}` |
| POST | /api/auth/pairing-code | → `{code, expires_at}` (authenticated; for pairing a phone) |
| POST | /api/auth/pair | `{code, label}` → `{csrf_token}` + cookie |

## Conversation, voice, commands
| Method | Path | Body → Response |
|---|---|---|
| POST | /api/conversations | → `{id}` |
| GET | /api/conversations/{id}/messages | → `[Message]` where Message=`{id, role, text, spoken_text, capture_id, proposal_id, created_at}` |
| GET | /api/voice/status | → `{engine, model, device, state, downloaded, download_size_mb, load_ms, warmup_ms, error, local_only, hardware, last_latency_ms}` |
| POST | /api/voice/warmup | → same as status (may take a while on first download) |
| POST | /api/voice/sessions | `{mode:"push_to_talk"\|"hands_free", conversation_id}` → `{id, max_duration_s}` |
| POST | /api/voice/sessions/{id}/end | → `{ok}` |
| POST | /api/captures/audio | multipart: `file`, `captured_at`, `tz`, `conversation_id`, `voice_session_id?`, `retain_audio?` ("true"/"false") → `Capture` |
| POST | /api/captures/{id}/transcript | `{text}` → `Capture` (new `user_edit` version) |
| POST | /api/commands | `CommandRequest` → `CommandResponse` |
| POST | /api/runs/{id}/cancel | → `{ok, status}` |
| GET | /api/runs/{id}/events | → `[RunEvent]` |

`Capture` = `{capture_id, status, audio:{mime, bytes, duration_s, format}, transcript:{version, text, source, confidence, latency_ms, model} | null, error:{code, message} | null}`.
Audio error codes (HTTP 422): `stt_unavailable`, `decode_failed`, `too_large`, `too_long`, `empty_audio`, `unsupported_format`, `no_speech`.
The UI must show these distinctly from reasoning errors and keep the typed input usable.

`CommandRequest` = `{conversation_id, text, channel:"voice"|"typed", capture_id?, transcript_version?, captured_at, tz, speech_playing:boolean, attachments?:[document_id]}`
(`captured_at` = when the user started speaking / pressed send; `speech_playing` = Jarvis TTS was audible during capture.)

`CommandResponse` = `{run_id, status, message:Message, proposal:Proposal|null, questions:[string], citations:[Citation], events:[RunEvent], intent:string, specialists:[string], visuals:[Visual]}`

`Visual` = one card payload discriminated by `type`: `calendar` (days, events, open windows, now line) | `weather` | `tasks` | `slots`
(shapes in `agents/visuals.py`).

`RunEvent` = `{id, run_id, kind, message, data, created_at}`. `Citation` = `{document_id, title, locator, excerpt, segment_id}`.

## Proposals & actions
`Proposal` = `{id, status, title, version, hash, summary, assumptions:[string], questions:[string], expires_at, presented_at,
  items:[ProposedItem], versions:[{version, hash, created_at, superseded_at}], approval:{id, selected_items, channel, created_at}|null}`

`ProposedItem` = `{index, action_type, provider, account_label, target:{id, name}, args, before, after, expected_effect, reason,
  missing_fields:[string], state, external_id, external_url, last_error, receipt:{verified_at, method, ok, simulated, observed}|null}`

States: proposal `proposed|approved|queued|executing|verified|partial|failed|unknown|rejected|expired|superseded|cancelled`;
item `proposed|approved|queued|executing|verified|failed|unknown|conflict|cancelled|skipped|superseded|paused`.

| Method | Path | Body → Response |
|---|---|---|
| GET | /api/proposals?status=active\|all | → `[Proposal]` |
| GET | /api/proposals/{id} | → `Proposal` |
| POST | /api/proposals/{id}/approve | `{version, hash, selected_items:[int]}` → `{approval_id, proposal}` (409 if stale version/hash, 423 if paused) |
| POST | /api/proposals/{id}/reject | → `Proposal` |
| POST | /api/proposals/{id}/revise | `{items:[{index, args?, remove?:bool}]}` → `Proposal` (new version; prior approval void) |
| POST | /api/proposals/{id}/retry | `{items:[int]}` → `Proposal` (only failed/unknown items; unknown reconciles first) |
| GET | /api/actions/history?limit= | → `[{action..., proposal_id, proposal_title, approved_at, receipt, attempts:[...]}]` |
| GET | /api/control | → `{actions_paused, paused_at, reason, in_flight:[{action_id, summary, state}]}` |
| POST | /api/control/pause | `{reason?}` → control |
| POST | /api/control/resume | → control (does NOT replay previous approvals) |
| GET | /api/policies | → `{rules:[Rule], catalog:[{action_type, label, provider, tier, automatable, undo, reaches_others}], undo_window_minutes}` |
| POST | /api/policies | `{action_type, label, match?:{target_resource?, args?}, origins?:["command"\|"mission"\|"channel"], daily_cap?, expires_at?}` → `Rule` (400 `not_t1` for T2/T3 types) |
| DELETE | /api/policies/{id} | → `Rule` (revoked) |
| POST | /api/actions/{action_id}/undo | → `{proposal_id, idempotent, proposal}` (410 once the undo window passed; 409 if not undoable) |

`Rule` = `{id, action_type, action_label, label, match, origins, daily_cap, used_24h, created_at, expires_at, revoked_at, active}`.
Approval `channel` is `button|voice|typed|policy|telegram|discord`. History items add `auto_approved`, `undo_until`, `undone_by`.

## Runs (live progress)
| Method | Path | Body → Response |
|---|---|---|
| GET | /api/runs/{id} | → `{id, conversation_id, kind, status, specialists, cancel_requested, error, started_at, finished_at}` |
| GET | /api/runs/{id}/events | → `[RunEvent]` |
| GET | /api/runs/{id}/stream?after= | `text/event-stream`: `run_event` (`RunEvent`, SSE `id` = event id), then `done` (run view). Waits ≤15 s for the run to appear; ends after 600 s with `timeout` (reconnect with `after`). |

## Today, plans, briefings, schedules, notifications
| Method | Path | Body → Response |
|---|---|---|
| GET | /api/weather | → weather `Visual` for today (current + 7-day), `{type:"weather", disabled:true}` when off; 503 if the forecast is unavailable |
| GET | /api/today | → `{date, tz, now, plan:{mission, priorities, constraints, first_action, version, author}\|null, agenda:[Event], tasks_due:[Task], deadlines:[Deadline], conflicts:[string], work_windows:[{start,end,label}], sources:{todoist, calendar, deadlines: {status:"ok"\|"stale"\|"unavailable"\|"not_connected", last_sync, detail}}}` |
| PUT | /api/plan/today | `{mission, priorities:[string], constraints?:[string], first_action?}` → plan |
| GET | /api/plan/history?date= | → `[plan version]` |
| GET | /api/briefings?kind=&limit= | → `[Briefing]` |
| POST | /api/briefings/generate | `{kind:"daily_command"\|"nightly_debrief"\|"weekly_war_room", force?:bool}` → `Briefing` |
| POST | /api/debriefs/nightly | `{briefing_id?, answers:{completed:[string], changes:string, carryover:[string], notes:string}}` → debrief |
| GET | /api/schedules | → `[Schedule]` = `{id, key, workflow, label, tz, hour, minute, weekdays, enabled, destination, next_run_at, last_run_at, last_status, last_success_at}` |
| PATCH | /api/schedules/{id} | `{enabled?, hour?, minute?, weekdays?, destination?}` → Schedule |
| GET | /api/worker/status | → `{alive, last_beat_at, worker_id, pid}` |
| GET | /api/notifications | → `[{id, kind, title, body, ref_type, ref_id, created_at, read_at, push_status, opened_at}]` |
| POST | /api/notifications/{id}/read | → `{ok}` ; `/opened` likewise |
| GET | /api/push/config | → `{supported, vapid_public_key\|null, reason?}` |
| POST | /api/push/subscribe | `{endpoint, keys:{p256dh, auth}}` → `{id}` ; DELETE same path `{endpoint}` |

`Briefing` = `{id, kind, for_date, content:{headline, mission, priorities:[..], agenda:[..], due:[..], deadlines:[..], windows:[..], conflicts:[..], obstacle, first_action, sections:[{title, items}]}, spoken_summary, sources, generated_by, catch_up, created_at}`

## Memory & knowledge
| Method | Path | Body → Response |
|---|---|---|
| GET | /api/memories?q=&status=&project_id= | → `[Memory]` = `{id, type, status, content, version, pinned, confirmed_by_user, project_id, entity, source_kind, source_ref, observed_at, review_at, expires_at, superseded_by, conflict_with, versions?}` |
| POST | /api/memories | `{content, type, project_id?, pinned?, entity?}` → Memory |
| PATCH | /api/memories/{id} | `{content}` → Memory (correction: new version) |
| POST | /api/memories/{id}/forget | → `{ok, note}` ; `/pin` `{pinned}` |
| GET | /api/memories/export | → markdown download |
| GET | /api/projects | → `[{id, slug, name, area, description, notion_url, sensitive, next_actions:[..], memories_count}]` |
| PATCH | /api/projects/{id} | `{description?, notion_url?}` |
| POST | /api/documents | multipart `file`, `project_id?` → `Document` |
| GET | /api/documents | → `[Document]` = `{id, title, mime, origin, project_id, created_at, version, sha256, status, processing:{stages..., visual:{images, rendered_pages, needs_vision}}, segments}` |
| GET | /api/documents/{id} | → Document + `segments:[{id, locator, kind, excerpt}]` |
| DELETE | /api/documents/{id} | → `{ok}` |
| GET | /api/search?q=&project_id= | → `[Citation & {kind, score}]` |
| POST | /api/evidence/preview | `{document_ids:[..], question}` → `{blocks:[{type:"text"\|"image", locator, chars?\|bytes?, media_type?}], estimated_input_tokens, leaves_machine:true}` |

## Integrations, local operator, settings, diagnostics
| Method | Path | Body → Response |
|---|---|---|
| GET | /api/integrations | → `[{provider, account_id, status, label, scopes, last_sync_at, last_error, grants:[{resource_type, resource_id, resource_name, access, is_default_target}], live_verified:bool}]` |
| POST | /api/integrations/todoist | `{token}` → integration |
| GET | /api/integrations/todoist/projects | → `[{id, name}]` |
| POST | /api/integrations/todoist/target | `{project_id}` → integration |
| POST | /api/integrations/todoist/sync | → `{pulled, reconciled}` |
| GET | /api/integrations/google/setup | → `{configured, redirect_uri, required_apis, scopes:{calendar, drive}}` |
| POST | /api/integrations/google/start | `{purpose:"calendar"\|"drive"}` → `{auth_url}` |
| GET | /api/integrations/google/callback | (browser redirect) |
| GET | /api/integrations/google/calendars | → `[{id, summary, accessRole, primary, selected_read, write_target}]` |
| POST | /api/integrations/google/calendars | `{read:[id], write_target:id\|null}` |
| POST | /api/integrations/drive/roots | `{ids:[string]}` ; GET lists roots |
| POST | /api/integrations/ics | `{url}` → integration (URL stored encrypted, never echoed) |
| POST | /api/integrations/ics/refresh | → `{items, changed, last_refresh}` |
| GET | /api/deadlines | → `[Deadline]` ; GET/POST /api/course-mappings |
| POST | /api/integrations/github | `{token, repos:[string]}` ; GET /api/github/status |
| POST | /api/integrations/anthropic | `{api_key}` |
| DELETE | /api/integrations/{account_id} | disconnect (revokes where supported) |
| GET | /api/localops/roots ; POST `{path, label, writable}` ; DELETE /api/localops/roots/{id} |
| GET | /api/localops/list?root_id=&path= ; /read ; /search?root_id=&q= |
| GET | /api/settings ; PATCH `{display_name?, timezone?, voice?:{voice_name, rate, muted, auto_speak}, privacy?:{retain_audio}}` |
| GET | /api/budget ; PATCH `{paid_inference_enabled, background_paid_enabled, daily_limit_usd, monthly_limit_usd, max_output_tokens, max_tool_calls, max_specialists, effort}` → `{..., spent_today_usd, spent_month_usd, reserved_usd, pricing, measured_vs_estimated}` |
| GET | /api/doctor | → `{checks:[{name, status:"ok"\|"warn"\|"fail"\|"pending", detail, remedy}]}` |
| GET | /api/metrics/latency | → `{kind: {count, p50_ms, p95_ms, last_ms}}` |
| GET | /api/activity?limit= | → `[{at, kind, message, run_id?, entity_type?, entity_id?}]` |
| POST | /api/backups | → `{id, path, sha256, bytes, schema_version}` ; GET /api/backups |
| POST | /api/backups/{id}/restore-preview | → `{compatible, counts, will_disable:[..], pre_restore_backup:true}` |
| POST | /api/backups/{id}/restore | `{confirm:true}` → `{ok, pre_restore_backup_id}` |

## V4 endpoints (all session-authenticated unless noted; writes need `X-CSRF-Token`)
| Area | Method | Path | Notes |
|---|---|---|---|
| Email | GET | /api/inbox?unread_only&limit&q | `{items:[MailItem], counts, sources, mailboxes}`; a failing mailbox is `unavailable`, never "empty" |
| | GET | /api/inbox/thread?ref= | thread text, labeled untrusted |
| | GET/POST | /api/integrations/microsoft/setup, /start `{with_send}` | Outlook OAuth (PKCE); callback is public: `/api/integrations/microsoft/callback` |
| | POST | /api/integrations/mail/sending `{mailbox, enabled}` | creates/removes the `mail_send` grant |
| Apps | GET | /api/connectors | catalog: fields, steps, reads, writes, status |
| | POST | /api/connectors/{id}/connect `{values}` | token connectors |
| | GET/POST | /api/connectors/{id}/resources `{read?, write?}` | allowlist picker (write requires read where both exist) |
| | POST | /api/connectors/spotify/start | callback (public): `/api/integrations/spotify/callback` |
| MCP | GET/POST | /api/mcp | add `{name, transport: stdio\|http, command?, args?, url?, env?, bearer?}` → discovers tools |
| | POST | /api/mcp/{id}/discover, /review `{modes:{tool:{mode,tier}}}`, /enabled `{enabled}` | review pins the tool-list hash |
| | DELETE | /api/mcp/{id} | |
| Vault | GET/POST | /api/vault, /api/vault/roots/{id} `{enabled}`, /reindex | |
| Missions | GET | /api/missions | `{missions, templates, families, read_only_families}` |
| | POST/PATCH/DELETE | /api/missions[/{id}] | schedule `{kind: daily\|weekly\|interval, at?, days?, minutes?}` |
| | POST | /api/missions/{id}/run, /enabled | run is queued for the worker |
| Telegram | GET/POST | /api/channels/telegram `{token}`, /pair, /unpair, /notify `{notify}` | |
| Sandbox | GET | /api/sandbox, /api/sandbox/history, /api/sandbox/shots/{id} (PNG) | |
| | POST | /api/sandbox/start, /stop, /reset, /build, /network/close, /settings `{computer_use}` | |
| Skills | GET/POST/DELETE | /api/skills, /install `{folder}`, /{name}/enabled, /{name} | |
| Today | GET | /api/headsup, /api/report/weekly | proactive cards; weekly estimate with stated basis |
| Help | GET | /api/manual, /api/manual/search?q= | guides + reference generated from registries |
| Activity | GET | /api/activity?actor= | user\|policy\|mission\|channel\|system\|worker\|model |
