# JARVIS architecture

```
Browser/PWA (React)  ──HTTPS/loopback──▶  FastAPI (jarvis serve)  ──▶  SQLite (WAL, FKs, FTS5)
  mic → MediaRecorder                          │  auth, CSRF, host/origin checks        ▲
  speechSynthesis (TTS)                        │  chief → specialists (read-only)       │ leases
                                               │  proposals + approvals (outbox)        │
                                               ▼                                        │
                                    local faster-whisper (CPU int8)          jarvis worker (separate process)
                                                                              ├ execute_action → executor → adapters → Todoist / Google
                                                                              ├ scheduler.tick → briefings → notifications / Web Push
                                                                              └ ics_refresh / todoist_sync (hourly/15 min; no LLM polling)
```

## V2 brain (agent loop)
When an Anthropic key and budget are set, every command except approvals, "sounds good", cancels and plan corrections goes to `agents/agent.py`. Those four always stay deterministic.

The agent loop:
1. **System prompt:** Jarvis persona, current date and time, today's accepted mission, relevant memories, and which data sources are connected.
2. **Tools:** registered in `agents/tools/` (V4 registry: each `Tool` has a schema, risk class `read|propose|act`, specialist family, availability check and Help text). Currently 20 client tools (calendar, free time, tasks, deadlines, weather, memory, documents, GTA plan, briefings, GitHub, workspace files, and the `propose_*` tools), plus Anthropic server tools `web_search_20260209` and `web_fetch_20260209`. Web research can be switched off in Settings. Tools marked `deferred` are sent with `defer_loading: true` behind `tool_search_tool_bm25_20251119`, so integration/MCP tools don't bloat every request.
3. **Loop:** up to 14 model steps, 30 tool calls and 240 s (15 web searches by default, adjustable 1–50; 12 fetches). It handles `pause_turn` and refusals, appends full response content each step (thinking and server-tool blocks round-trip unchanged), and uses automatic prompt caching.
4. **Code-level rails:**
   - `propose_*` tools only build `ActionSpec`s, which become ONE approval card.
   - `when_phrase` must appear verbatim in AJ's message and is resolved by `timeparse`, so the model cannot invent a date.
   - `save_memory` only works when AJ said "remember…".
   - Tool, web and document text is labeled untrusted.
   - There is no tool that approves, executes or deletes.
5. **Budget:** each step reserves worst-case cost (tokens plus web searches at $0.01 each) and reconciles measured usage, including `server_tool_use.web_search_requests`. A budget or model error falls back to the offline router with a visible note.
6. **Live progress:** the UI sends `client_run_id` and subscribes to `GET /api/runs/{id}/stream` (server-sent events), falling back to polling `/api/runs/{id}/events`.

## V4 autonomy (actions/registry.py, actions/policy.py)
- **Action-type registry:** the single allowlist of executable writes. Each type declares provider, handler, tier (T1 automatable / T2 always ask / T3 forbidden), idempotency (`native|marker|none`), required account + write grant, and its undo inverse. The executor's guard and proposal creation both read it.
- **Policy rules:** AJ creates them in the UI only. A rule pre-approves one T1 action type, optionally constrained to a target and exact arg values, for chosen proposal origins (`command` by default; `mission`/`channel` must be opted into), with a rolling 24 h cap. Auto-approval is all-or-nothing per proposal and goes through `proposals.approve(channel="policy")`, so every executor check still applies.
- **Undo:** reversible auto-approved actions get a 5-minute window. Undo creates the inverse action as a new proposal approved by AJ's click (idempotent on double-click).

Without a key or budget, the deterministic router below runs ("offline mode"). It no longer turns questions or look-ups into tasks.

## V4 operator layer
- **Integrations:** Gmail/Outlook (`integrations/gmail.py`, `microsoft.py`), app connectors from one catalog
  (`integrations/connectors.py`: Notion, Slack, Canvas, Spotify, Home Assistant, OpenRouteService, SimpleFIN), MCP hub
  (`integrations/mcp_hub.py`). Each adapter has a labeled fake and registers its action types with tiers.
- **Missions** (`missions/service.py`): scheduled agent runs with `agent.Scope` (families, proposals, origin, budgets).
- **Channels** (`channels/telegram.py`): outbound polling, one paired chat, hash-bound approval buttons.
- **Sandbox** (`sandbox/`, `backend/sandbox/`): Docker container + egress allowlist proxy; computer toolset handled in
  the agent loop; skills (`skills/service.py`) loaded as deferred tools.
- **Help** (`manual/`): hand-written guides + reference generated from the registries; `search_manual` tool.
- **Today heads-up** (`headsup.py`): deterministic proactive cards (approvals, leave-by, conflicts, deadlines, email).

## Command flow
1. **Capture** — audio → `/api/captures/audio` (PyAV decode: WebM/Opus, MP4/AAC, Ogg, WAV, MP3; size ≤ 25 MB, ≤ 300 s) →
   local STT → `TranscriptVersion` (editable; edits create new versions). Raw audio is discarded unless AJ opts in.
2. **Understand** — `agents/interpret.py` (deterministic, always available): intent (capture, schedule, correct, approve,
   cancel, remember, brief/debrief/war room, gta queue, knowledge, meeting debrief, local file, question…), commitment
   splitting, relative dates via `core/timeparse.py` anchored to the capture instant + tz (due vs do-date vs preferred
   part of day vs duration; vague words never become deadlines). Optional model calls are budget-gated and validated.
3. **Route** — `agents/chief.py` picks one specialist (bounded: ≤3, persisted concise run events, no chain-of-thought).
4. **Propose** — `actions/proposals.py`: immutable `ProposalVersion` with SHA-256 hash of exact typed actions, target
   account/resource, tz, before/after, preconditions, reason, source, expiry (12 h). One active proposal per conversation.
5. **Approve** — button or explicit spoken/typed approval of a presented version (confidence gate, not during TTS playback,
   vague "sounds good" refused). Approval rows + execution jobs are written in one transaction (outbox).
6. **Execute** — worker leases job → `actions/executor.py` guards (see CLAUDE.md) → marks `executing` and commits → reconciles
   prior unknown outcomes → refreshes preconditions (free/busy, remote snapshot, etag) → adapter write with idempotency key
   (Todoist Sync `uuid`, Google client event id) → read-after-write → `VerificationReceipt`.
7. **Record/respond** — per-item states, receipts, task/calendar links, notifications, action history (asked → approved plan →
   changed resource → evidence → undo path).

State machine: `proposed → approved/rejected/expired/superseded → queued → executing → verified | failed | unknown | conflict`
(per item); proposal aggregates to `verified | partial | failed | unknown | cancelled`.

## Specialists (functional, bounded)
| Specialist | Module | Reads | Output |
|---|---|---|---|
| Chief/Briefing | `agents/chief.py`, `briefings/workflows.py` | context snapshot, DailyPlan | response, briefing, proposed DailyPlan |
| Tasks | `agents/specialists.py` | Todoist, task links, pending proposals | task proposals, duplicate hints |
| Calendar | `agents/specialists.py` | selected calendars, free/busy | event proposals with feasible slots / trade-offs |
| School/GTA | `specialists.gta_queue` | GTA events, deadlines (own vs gta), tasks, preference memory | ordered conditional queue |
| Training/Business | `specialists.area_summary` | project memories, tasks | summaries; no autonomous plan changes |
| Knowledge | `agents/knowledge_agent.py` | FTS evidence, attached docs (image blocks) | cited answer |
| Meeting Debrief | `agents/meeting.py` | uploaded notes/transcripts | decisions, commitments, unresolved owners, drafts (not sent), task proposals |
| Local Operator | `localops/` | designated roots | reads; write/move proposals with diff |

## Source-of-truth rules
- Todoist is the authoritative task list once connected; Jarvis stores only links/receipts (`task_links`), shown as
  `pending_sync`/`active`/`completed`/`deleted` after reconciliation (worker pulls every 15 min, and on demand).
- Google Calendar is authoritative for events; Jarvis never edits shared/other people's calendars (write target must be `owner`).
- DailyPlan is canonical per local date; only AJ (Today/PUT) accepts a mission; Daily Command may *propose* one if none exists;
  specialists quote the current version and never create competing missions.
- Memory authority: AJ's direct statement/edit (newest) > current project document > seed context > agent suggestion
  (stored as `hypothesis`). Equal-authority conflicts keep both (`conflict`) until AJ resolves.
- Deadline feed (ICS) is read-only, possibly incomplete, never "Canvas access". Items matching a calendar event are flagged
  `also_in_calendar` to avoid duplicate reminders. Course mappings must be confirmed by AJ.

## Permission matrix
| Operation | Needs | Enforced in |
|---|---|---|
| Transcribe, parse, draft, search, compute, save app state/memory | authenticated session | api/app.py |
| Read Todoist/Calendar/Drive/GitHub/ICS | connection + selected-resource grant | integrations/accounts.py, service.py |
| Todoist task create/update/complete/delete | approval of exact version + write grant on project | proposals.py, executor.py |
| Calendar event create/update/delete (no attendees, owned calendar) | approval + write grant + fresh free/busy/etag | executor.py |
| Workspace file write/move | approval of previewed diff + writable root + unchanged hash | localops/files.py |
| Email/SMS/Slack send, invites, purchases, grades, payments, shell, SSH | **not available** | ALLOWED_ACTIONS |
| Paid model call | key + chosen budget + atomic reservation | llm/budget.py |

## Data retention
- Audio: deleted immediately after transcription (never written to disk) unless AJ enables retention (Settings → Privacy).
- Transcripts, runs, proposals, receipts, audit events: kept locally until AJ deletes the data dir or restores a backup.
- Documents: originals content-addressed under `data/files`; deleting a document removes segments + FTS rows + unshared blobs.
- Forget memory: FTS rows removed and version text scrubbed; existing backups may retain it until deleted.
- Backups exclude the encryption key, `.env`, model weights and audio.

## Budget behavior
- Paid inference is OFF until AJ chooses a budget (daily limit required). Background (scheduled) paid calls need a separate toggle.
- Each call reserves worst-case cost (input estimate + max_tokens × price) atomically against daily/monthly limits,
  then reconciles measured `usage`. Unknown pricing ⇒ scheduled calls refused. Ledger ≠ provider billing (shown in UI).
- At a limit: capture, transcription, reads, proposals, approvals, execution, and drafts keep working; only model calls pause.
- Default effort `low`; briefing uses at most one short model call (headline polish) and is cached by input hash.

## Why no vector DB / Redis / LangGraph / n8n
SQLite FTS5 + metadata filters covers the current single-user corpus; embeddings would be added only after a measured
retrieval miss rate justifies them. The SQLite job table with leases replaces Redis. The chief/specialist router is plain
Python with explicit limits — no framework needed.

## Roadmap (not implemented; boundaries defined)
- **Broader computer control**: a separate, typed "computer-use" adapter behind the same approval executor, with per-app
  allowlists and screen-recording consent; never a generic shell.
- **Wake word**: on-device keyword spotter in a desktop companion app (browsers/iPhone can't listen in the background).
- **Richer TTS**: local neural TTS (Piper, `voice/tts.py`) is implemented. Next options: a custom-trained voice, or cloud TTS
  behind an explicit data-flow indicator.
- **More integrations**: Notion API (currently deep links), Gmail drafts (draft-only), Canvas API (read-only, if approved by AJ),
  MCP connectors behind the same scope/approval checks.
- **Embeddings**: only after an evaluated retrieval need.
