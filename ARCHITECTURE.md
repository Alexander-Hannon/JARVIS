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
