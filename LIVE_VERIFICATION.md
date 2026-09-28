# Live verification ledger

**Implemented** and **verified with AJ's live account** are separate statuses. As of 2026-09-28, **no live provider call has been made** because no credentials exist and live tests need AJ's approval.

## Status by capability
| Capability | Contract-tested | Live-verified | Device-verified | Notes |
|---|---|---|---|---|
| Local STT (faster-whisper base.en, CPU int8) | yes | **yes (real model, real speech)** | this WSL2 host | 8.7 s clip → ~0.8–2.2 s latency; model load ~19 s cold |
| Browser mic → MediaRecorder → upload → STT | yes | **yes** (headless Chromium, fake device playing a real WAV) | desktop Chromium | real Chrome/Edge microphone and iOS Safari **pending manual test** |
| WebM/Opus, MP4/AAC, WAV decoding | yes | **yes** (real encoded files) | — | iOS Safari's own recording format is not yet captured from a device |
| Browser speech output (TTS), interrupt, replay | UI code only | no | **pending** | depends on the OS voice list |
| Hands-free mode (VAD, time limit) | type-checked | no | **pending** | foreground only; no wake word |
| Todoist create/update/complete/delete + verify | yes (respx + fakes) | **pending** | — | needs token + test project |
| Google OAuth + Calendar read/free-busy/write | yes | **pending** | — | needs OAuth client + consent |
| Google Drive selected-file read | yes | **pending** | — | |
| ICS deadline feed | yes | **pending** | — | needs private feed URL |
| GitHub read-only status | yes | **pending** | — | optional |
| Anthropic reasoning/vision call | request blocks inspected (ScriptedLLM) | **pending** | — | needs key + budget choice + approval |
| Web Push delivery | code path only | **pending** | iPhone **pending** | needs VAPID + HTTPS + installed web app |
| Scheduler / worker without a browser | yes | **yes** (real worker process, heartbeat observed) | this host | always-on host not set up (deferred; see DEPLOYMENT.md) |
| Local workspace file ops | yes (real FS) | **yes** on a temp workspace | this host | no real AJ folder designated yet |
| Backup / restore | yes (real) | **yes** | this host | |

## Proposed live verification (needs AJ's explicit approval; nothing runs until then)
1. **Setup, no cost:**
   - Connect Todoist and create a dedicated project called **"Jarvis Test"**.
   - Connect Google and create a dedicated calendar called **"Jarvis Test"**, then select it as the write target.
2. **Bounded test plan.** All writes are synthetic, with no attendees, and are shown to you once before running:
   - Todoist: create 2 tasks, `JARVIS-LIVE-TEST 1` and `JARVIS-LIVE-TEST 2`, due tomorrow. Complete #1 and verify by read-back. Retry #2 with the same op to prove there's no duplicate.
   - Calendar: create one 45-minute event, `JARVIS-LIVE-TEST block`, tomorrow. Verify the event ID and ETag. Add a conflicting event manually, then attempt a second stale create and verify it is blocked.
   - Anthropic (only if you approve a cap, e.g. **$0.25 total**): one screenshot question on the synthetic timetable and one short headline call. Check the measured usage against the ledger.
3. **Cleanup:** delete only the resources created above, as one explicitly approved cleanup proposal.
4. Record the results below: request IDs, provider IDs, timestamps, cost.

## Results log
_(empty — no live runs yet)_
