# CLAUDE.md — JARVIS conventions and runtime safety boundaries

Source spec: `JARVIS_BUILD_SPEC.md` (authoritative). Progress: `BUILD_STATUS.md`. HTTP contract: `docs/API_CONTRACT.md`.

## Layout
- `backend/` — Python 3.12, uv. `src/jarvis/`: `api/` (FastAPI routes + security middleware; V4 routes in `v4.py`),
  `agents/` (interpreter, chief, specialists, knowledge, meeting; agent tools registered in `agents/tools/`),
  `actions/` (proposals, executor, action-type `registry`, autonomy `policy`), `integrations/` (adapters, fakes, accounts/grants,
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
V4 plan (approved by AJ): `~/.claude/plans/alright-so-let-s-go-polymorphic-wilkes.md`.
- The ONLY executable external writes are the types registered in `actions/registry.py` (each declares provider, handler,
  tier, idempotency, undo). Anything not registered cannot be proposed or executed. Currently registered: Todoist task CRUD/
  complete, personal Google Calendar events without attendees, workspace file write/move.
- Tiers: T1 = may be auto-approved by an AJ-authored rule (`actions/policy.py`); T2 = always explicit approval; T3 = never.
  Hard T2 floor: anything reaching other people (send/post/invite), deletes, money, locks/garage/alarm, network-enabled
  sandbox runs. `register()` refuses a T1 type with `reaches_others=True`.
- Policy rules are created ONLY via the authenticated UI (`POST /api/policies`); no agent tool can create rules,
  approvals, or undo. Auto-approval is all-or-nothing per proposal, origin-scoped (default: AJ's own commands only),
  daily-capped, and produces a normal Approval (channel `policy`) bound to version+hash+items.
- Every provider write goes through `actions/executor.py` after application-code checks: approval bound to version+hash+
  selected items, current version, freshness, pause state, registry, account identity, resource write grant, preconditions.
  Model output can never create Approval rows or call adapters directly.
- Email send and Slack channel posts are T2 + `reaches_others` (email send also needs the `mail_send` grant AJ toggles).
  Home Assistant tier is derived from the entity domain (locks/alarms/covers/scripts → `home.secure`, T2) and re-derived
  by the executor. MCP tools (`integrations/mcp_hub.py`) are off until AJ reviews them; read mode only for server-marked
  read-only tools; everything else proposes `mcp.call` (T2) or `mcp.call.auto` (T1, never for destructive tools); tool
  definitions are hash-pinned and re-checked in the same session as every call.
- Sandbox (`sandbox/`, `backend/sandbox/`): shell/browser/computer use run ONLY in the `jarvis-sbx` Docker container
  (unprivileged, cap-drop ALL, read-only rootfs, no host mounts, `--internal` network). Its only exit is the
  `jarvis-egress` allowlist proxy, opened by an approved `sandbox.network` window (T2, domains + expiry). Files leave
  only via a `file.write` proposal. The computer toolset drives the sandbox display, never the host desktop.
- Skills are installed only by AJ (local folder) or from a draft he approved (`skill.install`, T2). No public registry.
- Missions run with `agent.Scope` (allowed families, proposals on/off, origin `mission`, per-run/monthly caps);
  Telegram commands run with origin `channel`, one paired chat, forwards stored as untrusted notes.
- Host shell, SSH, VM admin, purchases, grades, payments: NOT enabled.
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
