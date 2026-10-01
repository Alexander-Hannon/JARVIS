# BUILD_STATUS

## Session 7 update (2026-10-01): JARVIS V4 — M0 foundations
Plan approved by AJ: V4 "Operator" (native sandboxed OpenClaw-style powers, all four integration families, MCP client,
tiered autonomy). Milestones M0 → M4; this session landed **M0**.
- **Tool registry** (`agents/tools/`): every agent tool is a registered `Tool` (schema, risk `read|propose|act`, family,
  availability, Help text, example phrase). The 20 V2 tools moved over unchanged; dispatch no longer uses `getattr`.
  Deferred tools are sent with `defer_loading` behind `tool_search_tool_bm25_20251119`.
- **Action-type registry** (`actions/registry.py`): replaces `ALLOWED_ACTIONS` + `HANDLERS`. Each type declares provider,
  handler, tier (T1/T2/T3), idempotency, account + grant needed, and undo inverse. `register()` refuses T1 for anything
  that reaches other people.
- **Tiered autonomy** (`actions/policy.py`, migration `0003_v4_autonomy.sql`): AJ-authored rules (UI/API only) can
  auto-approve T1 actions — all-or-nothing per proposal, origin-scoped (AJ's own commands by default), 24 h caps,
  revocable. Auto-approvals are normal approvals (`channel='policy'`) so every executor guard still runs. 5-minute
  **Undo** for reversible auto-approved actions. Audit actor `policy`.
- **Live run streaming:** `GET /api/runs/{id}` and SSE `GET /api/runs/{id}/stream`; the UI uses EventSource and falls
  back to polling.
- **Brand setting:** `settings.brand.assistant_name` drives the agent persona name (commercial rename = one setting).
- **Docs:** CLAUDE.md safety section rewritten for tiers/registry; ARCHITECTURE.md V2 numbers fixed + V4 autonomy;
  API_CONTRACT adds policies, undo, runs, `/api/weather`, `visuals`.
- **Migration check:** `0003` applied to a *copy* of the real `data/jarvis.db`: row counts preserved (approvals 6,
  audit 100, proposals 12), `integrity_check` ok, no FK violations. The real DB is migrated on next `serve`/`worker` start.
- **Tests:** backend 253 passed (12 new V4: rule tiers, no-rule manual path, auto-approve → execute → undo, undo expiry,
  all-or-nothing + caps, target match, origin scoping + pause, revocation, registry invariants, tool search, SSE);
  ruff clean; frontend typecheck + build pass; UI (mocked) 15 passed.
- **Environment note:** this host lacks `libasound.so.2` for Playwright's Chromium; tests ran with the library extracted
  (not installed) into the session scratchpad via `LD_LIBRARY_PATH`. On your machine: `sudo npx playwright install-deps chromium`.
- **Still AJ's decision:** opting into server-side refusal `fallbacks` (fallback-model pricing may sit outside the local
  ledger) — left off.
- **M1 · Email (Gmail + Outlook):**
  - Adapters: `integrations/gmail.py` (REST over GoogleCredentials; `gmail.readonly` + `gmail.compose`, new Google
    purpose `gmail`) and `integrations/microsoft.py` (Microsoft identity auth-code + PKCE, tenant `common` so
    outlook.com works; Graph mail with `Mail.ReadWrite`; `Mail.Send` only when AJ reconnects with sending allowed).
  - Drafts are reconciled by a hidden marker (Gmail `X-Jarvis-Op` MIME header, Outlook single-value extended
    property), never by text in the visible body. Lost responses reconcile without duplicate drafts.
  - Action types per mailbox: `<box>.draft.create` (T1, undo = delete draft), `<box>.draft.delete` (T2),
    `<box>.send` (T2, `reaches_others`, idempotency `none`, needs a `mail_send` write grant that only exists while AJ
    has sending enabled; an unclear send is never re-sent: draft gone → treated as sent, draft present → AJ decides).
  - Agent tools (deferred behind tool search): `check_inbox` (deterministic triage: needs_reply/waiting/fyi/bulk),
    `search_email`, `read_email`, `propose_email_draft`, `propose_send_draft`. Rail in code: draft recipients must be
    in AJ's own words or on the thread he asked to reply to (blocks injected "send to attacker" addresses).
  - API: `/api/inbox`, `/api/inbox/thread`, Microsoft setup/start/callback, `/api/integrations/mail/sending`.
  - UI: Email card in Settings → Integrations (connect Gmail/Outlook, sending toggle with confirmation), inbox visual
    card in replies, letter-style email preview on approval cards (send items flagged "can't be unsent"; recipients
    not editable).
  - Not live-verified: needs AJ's Gmail API enablement / Azure app registration (see Blockers).
- **Tests:** backend 274 passed (+21: 12 mail flows incl. reconcile/no-resend/grant revocation/undo, 9 respx adapter
  tests); UI (mocked) 15 passed.
- **Next (M1 cont.):** Obsidian vault indexing, then Notion, Slack, Canvas, Spotify, Home Assistant, travel time,
  SimpleFIN — each with a contract fake, connect flow, grants and registered action types.

## Session 6 update (2026-09-30): full speech, research headroom, UI refinement
- **Speech cut off ~80% through:**
  - Cause: the spoken text was cut at 900 characters and the TTS endpoint capped input at 700.
  - Fix: caps removed. The client speaks in sentence-sized chunks and fetches the next chunk while the current one plays (pipelined), so it starts sooner and never truncates. Regression test added.
- **Search limits:** 15 web searches per command by default (adjustable 3–50 in Settings → Budget → JARVIS brain), 12 page fetches, 30 tool calls, 14 steps, 240 s. Searches are still counted in the budget at ~$0.01 each.
- **UI pass (frontend-design plugin):**
  - Signature **voice ribbon** on Command: a waveform from Jarvis's actual audio output (Web Audio analyser; procedural on iOS where routing audio through Web Audio is unreliable) with live captions of the sentence being spoken.
  - Today hero: greeting, plus "Happening now" / "Up next in N min" from the real calendar.
  - Typography: Chakra Petch in sentence case across the UI. Orbitron is kept only for the wordmark and clock, and mono only for times. All-caps labels are gone. Gold is reserved for attention.
  - Phone: compact status pills, slim ribbon, and the calendar timeline without nested chrome.
- **Tests:** backend 241 passed; UI (mocked) 15 passed. Desktop and 390×844 screenshots were reviewed against simulated data.

## Session 5 update (2026-09-30): conversational voice, visuals, phone polish
- **Conversational:** the V2 system prompt now makes Jarvis talk *to* AJ: natural first person, times said the way people say them, lists summarized because cards show the detail, and an optional short follow-up question.
- **Auto-speak on phones:**
  - One persistent audio element is unlocked on the first tap, which gets around iOS autoplay blocking.
  - Every reply is spoken automatically in the Jarvis voice (live-tested: the TTS request fires with no click, with clean text).
  - Fixed a bug found along the way: the audio unlock marked Jarvis as "speaking" at mic start, which would have blocked voice approvals.
- **Voice preferences:**
  - Speed is now synthesized by Piper (natural pitch), with a finer slider.
  - Prefs sync from the server on load, so the phone and laptop share them (live-tested with a fresh browser context).
- **Visual cards** (`agents/visuals.py`, `components/Visuals.tsx`), attached to replies from both V2 tools and offline handlers:
  - Calendar timeline: hour grid, overlapping events side by side, open windows, an all-day row and a "now" line.
  - Weather: current conditions plus a 7-day strip with SVG icons.
  - Tasks with due chips, and free slots.
  - The Today page shows the timeline and weather. The `/api/weather` endpoint was added.
- **Phone:**
  - 52 px orb.
  - One-row composer (attach, text, send).
  - Icon-only header buttons.
  - An animated speaking indicator.
  - Hidden badge clutter.
- **Tests:** backend 239 passed; UI (mocked) 15 passed; new real-backend voice tests 2 passed. Screenshots were checked on desktop and at 390×844.

## Session 4 update (2026-09-30): JARVIS V2 — agentic brain
- **Complaint:** commands defaulted to Todoist tasks. **Fix:** a Claude tool-use loop (`agents/agent.py`) now decides intent and picks tools, including web search and fetch. See ARCHITECTURE.md → "V2 brain".
- **Safety rails in code:**
  - Proposals only.
  - Dates must be verbatim from AJ's words.
  - No approve, execute or delete tools.
  - Approvals, cancels and corrections bypass the model.
  - Memory is saved only on "remember".
  - Budget reservation per step, web searches included.
- **Offline router:** "look up / research / find out / tell me…" are questions, never tasks.
- **Live progress** in the UI while the agent works. The Settings "JARVIS brain" card shows the mode and has the web-research switch.
- **Tests isolated from AJ's real `.env`:** his remote-mode setting had broken test logins.
- **Evidence:**
  - Backend: 237 passed, including 8 V2 tests with a scripted model. They cover tool routing, the invented-date rejection, web research with citations, approvals staying out of the model, the memory guard, budget fallback, and the offline no-task-default.
  - UI (mocked): 15 passed. Real-backend browser tests: 2 passed, repeated twice.
  - **Real API:** the exact V2 request (system prompt, 20 tools, web search/fetch definitions, `claude-opus-5-5`) was validated with the free `count_tokens` endpoint using AJ's key: valid, 9,900 input tokens.
  - **Not yet run:** a live agent command, because it spends money. AJ's first command is the live test.

## Session 3 update (2026-09-29): multi-request commands
- **Bug:** a sentence mixing a task, a calendar question and a weather question became three Todoist tasks. **Fix:**
  - `segment_utterance()` splits an utterance into separate requests and classifies each one.
  - Tasks merge into one approval card; calendar and weather are answered directly; everything comes back as one sectioned reply.
  - Approvals, corrections and cancels are still treated as a single whole utterance.
- **New specialists:**
  - Calendar rundown for any day, from the connected calendars.
  - Weather via Open-Meteo: free, no account; only the configured location's coordinates are sent (default Huntsville, AL).
- **Understanding:**
  - "go ahead and…" and "make a Todoist task (for me) to…" lead-ins are recognized.
  - The STT spelling "to-do-is" is normalized to Todoist.
  - Local STT is biased toward AJ's vocabulary (hotwords) for utterances of 3 s or more; short replies like "approve" are unaffected.
  - Whisper now loads offline when the model is already downloaded.
- **Speech:**
  - `speechify()` removes markdown, bullets, symbols, URLs and citations before anything is spoken, with a second cleanup in the browser voice.
  - Model prompts now ask for plain sentences.
  - The UI renders `▸` sections, bullets and **bold** properly instead of showing raw asterisks.
- **Layout:** the command dock is compact, so replies are no longer hidden behind the mic panel.
- **Evidence:**
  - Backend: 229 passed. That includes the reported utterance as typed text, and as real synthesized speech that transcribed word-for-word with "Todoist".
  - UI (mocked): 15 passed.
  - Real-backend browser tests: 2 passed, one of them against the live Open-Meteo API (returned tomorrow's forecast: overcast, 91°F / 68°F).

## Session 2 update (2026-09-29): fixes from AJ's first real use
- **Command splitting:** "Hey/Yo/OK Jarvis" greetings are stripped. "then", "after that" and mid-sentence lead-ins ("remind me to", "I also need to", "I want you to…") always start a new task. Trailing "thanks/please" is dropped. 9 regression tests in `tests/test_splitting.py`.
- **Unclickable checkbox:** the cause was that no Todoist destination project was chosen. Cards now show a "One step left" project picker. Choosing a project (here or in Settings) re-resolves open plans into a new version with the checkboxes unlocked. Tested.
- **Google OAuth:** the callback no longer needs the SameSite=Strict session cookie. It uses the single-use state plus a Lax browser-binding cookie. Tested.
- **Jarvis voice:**
  - Local neural TTS (Piper) with British male presets and an optional subtle "HUD" effect, served by `POST /api/voice/tts`.
  - A pronunciation lexicon handles Todoist, IS/MSC course codes, GTA and CCM.
  - Measured on this host: 0.27 s warm and ~1.1 s cold per sentence. The output round-trips through local STT at 0.94 confidence.
  - Falls back to browser voices automatically.
  - These are generic open voices, not a clone of any actor.
- **Seamless voice:**
  - One tap, speak, pause: silence detection stops the recording, transcribes it and sends it with no second click.
  - Hold still works as push-to-talk.
  - Review mode is available via the "Send automatically" toggle, and turns on automatically when STT confidence is low.
- **HUD UI:** cyan holographic theme, clipped-corner panels, arc-reactor mic that reacts to your voice level, live clock, and a *real* worker-heartbeat indicator. Bundled fonts (no Google Fonts requests). Reduced-motion respected.
- **Mobile fixes:**
  - The sticky header no longer covers items when scrolling to them.
  - The bottom nav stays above the panels.
  - The HUD type no longer overflows phone width.
- **Tests:** backend 218 passed; UI (mocked) 15 passed; real-backend browser test 1 passed (a single tap, then auto-stop on silence and auto-send); ruff clean.


_Last updated: 2026-09-28, end of build session 1_

## Environment (inspected)
- WSL2 Ubuntu (Linux 6.6), 8 CPU cores, 7 GB RAM, no GPU.
- Python 3.12.3 with uv 0.12; Node 24.16.
- No system ffmpeg; PyAV bundles its own FFmpeg libraries.
- Chromium system libraries are missing on this host. Playwright ran using libraries downloaded to the session scratchpad. On your machine, run `sudo npx playwright install-deps chromium`.

## Modules (spec §3)
| # | Module | Status |
|---|---|---|
| 1 | Voice/text console + speech output | Built. Real STT verified. Browser TTS and hands-free mode not device-tested. |
| 2 | Chief/router + bounded specialists | Built: Tasks, Calendar, Chief/Briefing, Meeting Debrief, School/GTA, Training/Business, Knowledge, Local Operator. |
| 3 | Proposal → approval → execution → verification → history | Built and contract-tested. |
| 4 | Todoist + Google Calendar | Real adapters and connection flows built. Live verification pending credentials and approval. |
| 5 | Memory, project memory, searchable evidence | Built (SQLite FTS5, versions, supersede/conflict, forget). |
| 6 | Daily Command / Nightly Debrief / War Room + durable schedules | Built. Worker-run without a browser. DST and misfire handling tested. |
| 7 | Local docs/images/PDF/DOCX/ipynb + selected Drive | Built. Visual blocks proven at request level. Live model response pending. |
| 8 | ICS deadline feed | Built (SSRF-guarded, encrypted URL, own/GTA mapping). Live feed pending. |
| 9 | Local operator (workspace roots) | Built and verified on a real filesystem. |
| 10 | Dashboard, diagnostics, costs, backup | Built: responsive PWA, doctor, budget ledger, backup/restore. |
| + | GitHub read-only; Obsidian/Markdown folder | GitHub adapter built. A Markdown folder can be used as a read-only workspace root today; auto-ingest into search is not built. |

## Test results (actual commands)
| Command | Result |
|---|---|
| `cd backend && uv run pytest -q` | 206 passed, including real faster-whisper on real speech |
| `cd backend && uv run ruff check src tests` | clean |
| `cd frontend && npm run typecheck && npm run build` | pass |
| `cd frontend && npx playwright test` | 14 passed (mocked API) |
| `cd frontend && npx playwright test -c playwright.live.config.ts` | 1 passed (real backend, real speech through Chromium's mic, no mocks) |
| Clean-copy install (`uv sync`, `npm ci`, build, pytest, serve) in a scratch directory | pass |

Measured on this host:
- STT: an 8.7 s utterance takes ~0.8 s via CLI and ~2.2 s via HTTP (including decode). The first model load takes ~19 s and downloads 142 MB.
- A command response on the deterministic path takes tens of milliseconds.
- Latency is also recorded live under `/api/metrics/latency`.

## Decisions
- **Interpreter:** deterministic and always available. Model calls are optional, gated by budget, and validated (date phrases must appear verbatim in what was said).
- **Todoist writes:** Sync API command `uuid` (the documented idempotency mechanism), plus a `[jarvis:op=…]` marker in the description for reconciliation.
- **Calendar writes:**
  - Deterministic client event ID plus a `jarvis_op` private extended property; a 409 means read back and verify.
  - Least-privilege scopes: `calendar.events.owned`, `calendar.events.readonly`, `calendar.calendarlist.readonly`, `calendar.freebusy`, openid, email.
  - Writes only go to calendars AJ owns.
- **Drive:** default scope is `drive.readonly`, with an application-layer allowlist of selected roots. The `drive.file` trade-off is documented.
- **STT confidence:** mean word probability. The voice-approval gate defaults to 0.55. Measured: "Approve." 0.58, "Change Thursday to Friday." 0.95. The gate has not been tuned on AJ's voice.
- **Anthropic refusal `fallbacks` parameter:** off by default. The fallback model's pricing may fall outside the local ledger; it can be opted into later.
- **Setup lockout:** the owner can only be created from loopback. There is no default password.
- **Audio:** never written to disk unless retention is opted into.
- **Deliberately not used:** vector DB, Redis, LangGraph, n8n, containers (see ARCHITECTURE.md).

## Blockers (need AJ)
0. V4 email: enable the Gmail API on the existing Google Cloud project (and add the two Gmail scopes to the consent
   screen); for Outlook, an Azure app registration (Settings → Integrations → Email shows the exact steps and redirect
   URI) with `JARVIS_MICROSOFT_CLIENT_ID/SECRET` in `.env`.
1. Todoist API token, plus a dedicated "Jarvis Test" project for live verification.
2. A Google Cloud OAuth Web client (ID and secret in `.env`) and consent; a "Jarvis Test" calendar.
3. Private ICS deadline-feed URL; confirm course mappings (own vs GTA).
4. Optional: Anthropic API key, a budget choice, and approval of a bounded live test (proposed: $0.25 cap).
5. A device test on iPhone (HTTPS via Tailscale, mic, installed web app, Web Push).

## Next actions
1. After credentials: run the bounded live plan in LIVE_VERIFICATION.md and record the results.
2. On-device checks: Chrome/Edge mic with your own voice (tune `JARVIS_STT_MIN_CONFIDENCE`), TTS voices, and a hands-free session.
3. Frontend contract refinements requested by the UI build:
   - Allow `revise` to edit assumptions.
   - Add `GET /api/captures/{id}` and `GET /api/runs/{id}`.
   - Server-sent streaming of run events. Today, events come back with the response; the deterministic path is fast, but model calls aren't streamed.
4. Tell AJ which fragments were dropped. The interpreter currently drops dangling verb-only fragments (such as a trailing "I need to grade") silently.
5. Obsidian/Markdown: add "index this workspace folder into search" through the existing ingestion pipeline.
6. Optional: an always-on host (DEPLOYMENT.md §2).
