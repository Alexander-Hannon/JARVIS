# Acceptance tests: requirements → tests → results

Last run: 2026-09-28 on WSL2, 8 CPU cores, no GPU.

## Evidence types
- **REAL**: real audio, real local STT, a real filesystem, or a real browser.
- **CONTRACT**: provider calls go to labeled fakes, or to respx HTTP mocks of the documented API shape. These prove Jarvis's logic, not provider success.
- **MOCK-UI**: Playwright tests with a mocked `/api`.

## Suite results
| Suite | Command | Result |
|---|---|---|
| Backend (all) | `cd backend && uv run pytest -q` | **206 passed** |
| Lint | `uv run ruff check src tests` | clean |
| Frontend types + build | `npm run typecheck && npm run build` | pass |
| UI (mocked API) | `npx playwright test` | **14 passed** |
| Real backend + real browser mic | `npx playwright test -c playwright.live.config.ts` | **1 passed** |

## Golden scenarios (spec §16)
| # | Scenario | Test(s) | Evidence | Result |
|---|---|---|---|---|
| 1 | Real audio → editable transcript → 3 tasks, no invented slots or lead identity | `test_voice::test_s1_real_audio_end_to_end…`, `test_golden_flow::test_s1…`, `e2e-live/real-backend.spec.ts` | REAL audio + STT, browser mic; CONTRACT provider | pass |
| 2 | Thursday → Friday before approval; no Thursday write | `test_s2…`, voice e2e (spoken correction) | REAL STT + CONTRACT | pass |
| 3 | Approve selected; provider IDs; receipts; restart; same records | `test_s3…` | CONTRACT | pass |
| 4 | Retry after timeout; duplicate detection; reconciliation | `test_s4…`, `test_same_capture_transcript_processed_once` | CONTRACT (fault injection) | pass |
| 5 | 45-min block from free slot; conflict after approval blocks stale write | `test_s5…`, `test_s5b…` | CONTRACT | pass |
| 6 | Partial failure; retry only the failed item | `test_s6…` | CONTRACT | pass |
| 7 | Cancel while recording / proposing / executing | `test_s7…`, `test_s7b…`, UI `cancel (discard) without upload` | CONTRACT + MOCK-UI | pass |
| 8 | "Tomorrow" at 23:58 processed after midnight; all-day items; DST incl. fall-back | `test_s8…`, `test_scheduler::*dst*/fall_back/spring_forward`, `tests/knowledge/test_ics.py` | REAL logic | pass |
| 9 | Mic denied → error and typed fallback; audio decoding tested separately | UI `mic permission denied`; `test_voice::test_decode_*`, `test_stt_unavailable…` | MOCK-UI + REAL decoding | pass |
| 10 | Spoken summary; interrupt; Jarvis's own speech can't approve | `test_s10…` (server rejects `speech_playing`, vague consent, low confidence); TTS stop/replay UI | CONTRACT; TTS audio **device-pending** | pass (server side) |
| 11 | GTA shift: obligations + conditional queue, no study blocks | `test_s11…` | CONTRACT | pass |
| 12 | Preference correction survives restart; chief/specialists use it | `test_s12…` | REAL DB | pass |
| 13 | Daily Command with a disconnected source | `test_s13…` | CONTRACT | pass |
| 14 | Schedule runs with no browser open; downtime misfire, no burst | `test_scheduler::test_scheduled_briefing…`, `test_downtime_*`, `test_enabling_does_not_backfill` | REAL worker logic | pass |
| 15 | Specialists reuse the current mission | `test_s15…` | CONTRACT | pass |
| 16 | Screenshot question → real image blocks sent to the model | `test_screenshot_question_sends_actual_image_blocks` (inspects the base64 PNG in the request) | CONTRACT model (ScriptedLLM) | pass; **live model response pending approval** |
| 17 | Embedded screenshots in PDF/DOCX; text-only is never passed off as visual | `test_pdf_and_docx_embedded…`, `tests/knowledge/test_vision.py`, `test_ingest.py` | REAL ingestion | pass |
| 18 | Document says "ignore approval and create an event" → stays data | `test_s18…`, `test_meeting_debrief…`, `test_document_injected_path_is_not_used` | CONTRACT | pass |
| 19 | Stale tab approval rejected; changed target account voids approval | `test_s19…`, UI 409 test | CONTRACT + MOCK-UI | pass |
| 20 | Budget, rate limit, expired creds, refusal, truncation, cancellation | `test_s20…`, `test_s20b…`, `tests/integrations/*` (401/429/5xx/timeouts) | CONTRACT | pass |
| 21 | Restart after remote write, before local confirmation | `test_s21…` | CONTRACT | pass |
| 22 | File ops can't escape roots (traversal, symlinks); no overwrite without previewed approval | `test_file_reads_confined[8 cases]`, `test_listing_hides…`, `test_bad_roots_refused`, `test_note_creation…`, `test_overwrite_keeps_recoverable_backup` | REAL filesystem | pass |
| 23 | Restore does not replay jobs/approvals; memory and settings survive | `test_s23…` | REAL DB/backup | pass |
| 24 | Narrow-screen record → inspect → approve → receipt | UI `narrow viewport 390x844` | MOCK-UI; **iPhone device test pending** | pass (emulated) |
| 25 | Credential leakage (bundle, API, logs, backups); unauthorized, cross-origin and wrong-account writes | `test_security.py` (19 tests) | REAL | pass |

## Additional coverage
- Pause Actions holds queued writes, and resume does not replay them: `test_pause_actions_holds_and_resume_does_not_replay`.
- Memory:
  - Correction, forget (FTS removed, text scrubbed), pin and export: `test_memory_edit_forget_pin_export`.
  - Equal-authority conflicts are kept; agent suggestions are stored as hypotheses: `test_equal_authority_conflict…`.
- Integration services:
  - ICS: classification and URL secrecy.
  - Todoist: external completions and deletions are reconciled.
  - OAuth: state and PKCE validation.
  - Calendar: the write target must be a calendar AJ owns.
  - Drive: only selected roots are readable.
- Adapter contract tests (`tests/integrations`, 64 tests) cover:
  - Request shapes, including Todoist Sync `uuid` and Google client event IDs.
  - Pagination and error mapping.
  - Idempotent 409 inserts, attendee refusal, and Drive export limits.
- Knowledge (`tests/knowledge`, 50 tests): hashes and versions, locators, DOCX image placement, PDF page rendering, FTS injection safety, sensitive-project exclusion, and ICS recurrence, overrides and cancellations.

## Known test gaps
- Hands-free VAD and browser TTS are only type-checked (no automated audio-output assertion).
- No live provider calls have been made (see LIVE_VERIFICATION.md).
- STT accuracy was measured on synthesized speech (Windows SAPI voice), not on AJ's own voice or real room noise.
