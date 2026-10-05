# Incident response runbook (product side)

This is what an operator can do inside JARVIS during a suspected incident. The customer's own incident-response plan
owns detection, escalation and reporting, including DFARS 252.204-7012's 72-hour reporting window for covered
defense information.

## 1. Contain (minutes)
| Situation | Action |
|---|---|
| Agent or automation misbehaving | **Pause actions** (header button, or `POST /api/control/pause`). Queued provider writes stop being handed out; approvals are refused while paused. |
| Suspected stolen session or device | Settings → Security: sign out the device. Or run `uv run jarvis reset-password` on the host, which revokes **all** sessions. |
| Suspected credential leak (provider token) | Disconnect the integration in Connections. Its tokens are deleted and grants are removed. Then revoke the token at the provider (Google, Microsoft, Slack, …). |
| Suspicious MCP server or skill | Disable it in Settings → Integrations → MCP servers or Workbench → Skills. A changed tool definition already disables the server automatically. |
| Sandbox doing something unexpected | Workbench → Stop. Close any open internet window. `docker rm -f jarvis-sbx` is safe because the sandbox is disposable. |
| Brute-force sign-in attempts | They are throttled per IP automatically. Review Activity → auth events (each one has its IP). `jarvis doctor` flags more than 20 failures in 24 h. |

## 2. Preserve evidence
- Copy the data directory **before** changing anything: `uv run jarvis backup` (the encryption key is excluded by design).
- Export the audit trail: Activity → filter. (M2 adds a signed export and SIEM forwarding.)
- Keep the worker and serve logs. They contain no tokens or private URLs (redaction filter), but treat them as sensitive.

## 3. Eradicate and recover
- Rotate the encryption key if the host was compromised. Today this means reconnecting the integrations after
  replacing `secret.key`; M2 adds KMS-based rotation that re-wraps keys without reconnecting.
- Restore from a known-good backup if needed: `uv run jarvis restore <id> --yes`. Restore neutralizes pending jobs and
  approvals and disables schedules, so nothing fires on its own.
- Re-review MCP servers and skills before turning them back on.

## 4. Afterwards
- Record the incident and its timeline in the customer's system.
- Add a regression test for the failure mode (the `injection` marker for prompt-injection cases).
- Update `THREAT_MODEL.md` and the 800-171 mapping if a control turned out to be weaker than documented.
