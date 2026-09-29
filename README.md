# JARVIS

> **New here? Start with [GETTING_STARTED.md](GETTING_STARTED.md)** — a step-by-step setup and usage guide.

AJ Hannon's personal, voice-first operator. The core loop is **speak or type → local transcription → interpretation → proposal → approval → execution with a real adapter → read-back verification → receipt**. Briefings, memory, knowledge and local-file features are built on the same persistent state.

- Spec: `JARVIS_BUILD_SPEC.md`
- Status: `BUILD_STATUS.md`
- Design: `ARCHITECTURE.md`
- Hosting: `DEPLOYMENT.md`
- Test evidence: `ACCEPTANCE_TESTS.md` and `LIVE_VERIFICATION.md`

## Requirements
- Linux, macOS, or **Windows via WSL2** (Ubuntu). Run everything inside WSL, and open the UI in a Windows browser at `http://localhost:8765`, because WSL2 forwards localhost.
- Python 3.12 with [uv](https://docs.astral.sh/uv/). Node 20+ with npm.
- About 300 MB of disk for the local speech model, which is downloaded once from Hugging Face. There is no GPU requirement.

## Install (clean setup)
```bash
cd backend && uv sync                   # Python deps (locked in uv.lock)
cd ../frontend && npm ci && npm run build   # UI → frontend/dist, served by the backend
cd .. && cp .env.example .env && chmod 600 .env   # optional; defaults are loopback-only
cd backend && uv run jarvis setup-key   # creates data/secret.key (0600) for token encryption
uv run jarvis stt-warmup                # downloads faster-whisper base.en (~142 MB), prints measured warm-up
```

## Run (two processes)
```bash
cd backend
uv run jarvis serve     # API + UI on http://127.0.0.1:8765 (loopback only)
uv run jarvis worker    # separate terminal: executes approved actions, schedules, push, syncs
uv run jarvis doctor    # readiness report (STT, provider, OAuth, Todoist, Calendar, Drive, scheduler, DB, push)
```
Open `http://localhost:8765` **on the same machine**. The one-time owner setup is only accepted from localhost. To add a phone, go to Settings → Pair device, then see DEPLOYMENT.md.

Frontend dev server (hot reload, proxies `/api` to :8765): `cd frontend && npm run dev`.

## Connect accounts (in the UI: Settings → Integrations)
Each connection only authorizes **reads** of what you select. Every write still needs your approval.
- **Todoist:** paste a personal API token (Todoist → Settings → Integrations → Developer). Then choose the project Jarvis may add to. You can optionally map areas to projects.
- **Google Calendar/Drive:**
  - Create an OAuth *Web* client in your own Google Cloud project, using the exact redirect URI shown on the setup screen (`http://localhost:8765/api/integrations/google/callback`).
  - Put the ID and secret in `.env`, then restart and click Connect.
  - Pick which calendars to read, and which **one** calendar you own receives Jarvis blocks.
- **Deadline feed:** paste your private ICS URL. It is stored encrypted and never shown again. Then confirm the course mappings (own vs GTA).
- **Anthropic** (optional): paste an API key, then choose a budget. Paid inference stays off until you do.
- **GitHub** (optional): a fine-grained read-only PAT with selected repos (Metadata, Issues, and Pull requests set to read).
- **Local workspace:** add a specific folder, read-only or writable. Your home directory, dot-folders and credential stores are refused.

## Tests
```bash
cd backend && uv run pytest -q                 # 206 tests, incl. REAL local STT on real speech (WAV/WebM/MP4)
uv run pytest -q -m "not slow"                  # skip the real-STT tests
uv run ruff check src tests
cd ../frontend && npm run typecheck && npm run build
npx playwright install chromium                 # first time; Linux may also need: sudo npx playwright install-deps chromium
npx playwright test                             # 14 UI tests with a MOCKED API (not live evidence)
# Real-backend browser test (no mocks; real speech fed into Chromium's mic). Start serve+worker on a FRESH data dir first:
JARVIS_DATA_DIR=/tmp/jarvis-e2e uv run jarvis serve   # (from backend/, plus worker)
npx playwright test -c playwright.live.config.ts
```
Provider calls in the tests go to contract fakes that are labeled `simulated`. They prove Jarvis's own logic, not live provider success. Live checks are a separate, approved step (see LIVE_VERIFICATION.md).

## Backup, restore, recovery
- Back up with `uv run jarvis backup`, or from Settings → Backups. This produces a `.tar.gz` of the DB and document files plus a sha256. It excludes the encryption key, `.env`, models and audio, so **back up `data/secret.key` separately and securely**. Without it, stored tokens can't be decrypted and you'd reconnect accounts.
- Restore with `uv run jarvis restore <id> --yes`, or through the UI's preview-then-confirm flow. Restore does the following:
  - Takes a pre-restore backup first.
  - Cancels queued jobs and revokes open approvals.
  - Disables schedules.
  - **Pauses actions**, so nothing old replays. Review, then resume.
- **Uncertain provider writes:** an item in state `unknown` may have reached Todoist or Google.
  - Use **Retry** on it. Jarvis first searches for its marker (`[jarvis:op=…]` in the Todoist description, or the deterministic Google event ID) and only writes if nothing is found.
  - Never recreate it by hand without checking the provider.
- **Pause actions** (header button) stops new writes immediately. Anything listed as executing or unknown may already have reached a provider. Resuming never replays held approvals; you re-approve.

## What leaves the machine
- **Audio:** never. Transcription is local.
- **Todoist, Google and GitHub:** requests go directly from this machine to those APIs.
- **Anthropic:** receives selected prompts, excerpts and images only when the model is enabled and budgeted. The UI previews visual evidence before sending.
- **Telemetry:** none.
