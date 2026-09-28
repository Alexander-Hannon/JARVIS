# CLAUDE.md — JARVIS conventions and runtime safety boundaries

Source spec: `JARVIS_BUILD_SPEC.md` (authoritative). Progress: `BUILD_STATUS.md`. HTTP contract: `docs/API_CONTRACT.md`.

## Layout
- `backend/` — Python 3.12, uv. `src/jarvis/`: `api/` (FastAPI routes + security middleware), `agents/` (interpreter, chief,
  specialists, knowledge, meeting), `actions/` (proposals, executor), `integrations/` (adapters, fakes, accounts/grants,
  connection services), `knowledge/` (ingest, vision blocks, FTS search, ICS), `memory/`, `briefings/` (workflows,
  scheduler, notify), `localops/` (workspace file tools), `voice/` (STT), `llm/` (provider, budget), `core/` (db, jobs,
  timeparse, seed, util), `worker.py`, `backup.py`, `doctor.py`, `cli.py`. Migrations: `src/jarvis/migrations/NNNN_*.sql`
  (forward-only; never edit an applied migration — add a new file).
- `frontend/` — React + TS + Vite PWA. Built `dist/` is served by the backend.

## Commands
- Backend tests: `cd backend && uv run pytest -q` (add `-m "not slow"` to skip the real-STT tests).
- Lint: `uv run ruff check src tests`. Frontend: `cd frontend && npx tsc --noEmit && npm run build && npx playwright test`.
- Run: `uv run jarvis serve` + `uv run jarvis worker`; diagnostics `uv run jarvis doctor`.

## Runtime safety boundaries (Jarvis's permissions, NOT Claude Code's)
- Enabled external write actions are ONLY those in `actions/proposals.py::ALLOWED_ACTIONS` (Todoist task CRUD/complete,
  personal Google Calendar events without attendees, workspace file write/move). Email/SMS/Slack send, invitations,
  purchases, grades, payments, shell, SSH, VM admin, unrestricted computer use: NOT enabled. Drafting only.
- Every provider write goes through `actions/executor.py` after application-code checks: approval bound to version+hash+
  selected items, current version, freshness, pause state, allowlist, account identity, resource write grant, preconditions.
  Model output can never create Approval rows or call adapters directly.
- Retrieved/uploaded/calendar/document text is untrusted data. It never grants permissions, picks destinations, or approves.
- Timeouts after writes are `unknown` outcomes: reconcile (marker / deterministic ID) before any retry. Never duplicate.
- Secrets: env or encrypted columns only (key outside DB). Never log, echo, export, or put in model context.
- Contract fakes (`integrations/fakes.py`, `ScriptedLLM`) are labeled `simulated`; they never count as live verification.
- Default network binding is loopback. Do not deploy publicly, open ports, or spend on runtime API calls without AJ's explicit approval.

## Conventions
- UTC instants in DB (`...Z`) + explicit IANA tz for local intent. Resolve relative dates against the capture time.
- One active proposal per conversation. Revisions create new versions; approvals never carry over.
- Status must be truthful: `not_connected` / `unavailable` / `stale` are rendered as such, never as "nothing due".
- Keep modules small and typed; match surrounding style; comments explain *why*.
