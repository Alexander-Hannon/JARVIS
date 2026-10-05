# JARVIS threat model

Scope: one JARVIS install (API + worker + SQLite/Postgres + sandbox) operated by one organization. Reviewed every
milestone. Last review: session 10 (V5 M0), 2026-10-05. Control references point to `NIST_800-171_MAPPING.md`.

## Assets
| Asset | Why it matters |
|---|---|
| Organization content: documents, email, memories, transcripts, meeting notes (may be **CUI**) | Disclosure is the primary harm. |
| Provider credentials: OAuth tokens, API keys, MCP secrets | Give access to systems far beyond JARVIS. |
| Authority to act: approvals, policy rules, the executor | Turn model output into real-world changes. |
| Audit trail | Evidence for incident response and assessments. |
| Model budget | Spend can be abused. |

## Trust boundaries
1. Browser/PWA ↔ API (session cookie + CSRF; HTTPS front in the remote profile).
2. API/worker ↔ model provider (prompt content leaves the install).
3. API/worker ↔ third-party providers (Google, Microsoft, Slack, …) through registered adapters only.
4. Agent ↔ untrusted content (web, email, documents, MCP output, Telegram forwards).
5. Host ↔ sandbox container (Docker, `--internal` network, egress proxy).
6. Operator console ↔ data directory (key file, `.env`, DB).

## STRIDE
| Threat | Example | Mitigations in place | Planned |
|---|---|---|---|
| **Spoofing** | Password guessing; stolen session cookie; forged OAuth callback | scrypt; per-IP throttles + known-device rule (M0); HttpOnly/SameSite=Strict/Secure cookies; idle + absolute session limits (M0); OAuth state + PKCE (verifier sealed, M0) + browser-binding cookie | MFA/WebAuthn, SSO (M1) |
| **Spoofing** | First-run setup taken over through the HTTPS proxy (every proxied request came from 127.0.0.1) | **Fixed M0:** proxied requests never count as loopback; the remote profile always needs the console setup token | — |
| **Tampering** | Edit a proposal after approval; replay an approval | Approvals bound to version + SHA-256 hash + selected items; the executor re-checks the current version, freshness and grants | Dual approval (M1) |
| **Tampering** | Modify audit rows | — | Hash chain, append-only role, signed checkpoints (M2) |
| **Repudiation** | "I didn't approve that" | Approval rows record channel + evidence; auth events record IP/user agent (M0) | Per-user identity on every event (M1) |
| **Information disclosure** | CUI sent to a non-authorized model endpoint | Paid inference is off until a budget is set | Classification labels + provider authorization routing (M2) |
| **Information disclosure** | SSRF to metadata/LAN through a user-supplied URL, incl. DNS rebinding | HTTPS-only, public-IP check, **DNS pinned per request (M0)**, redirects re-validated, size caps | Pin MCP HTTP transport too (M2) |
| **Information disclosure** | DB file or backup copied | Credentials encrypted with a key outside the DB; backups exclude the key | Envelope encryption of content, encrypted signed backups (M2) |
| **Information disclosure** | Secrets in logs | URL/token redaction filter; `from None` on chained errors | Structured logs + content redaction (M2) |
| **Denial of service** | Lock the owner out through the login throttle | Throttle is per IP; known devices are exempt from the account-wide limit (M0) | — |
| **Denial of service** | Budget exhaustion | Atomic worst-case reservation against daily/monthly limits | Per-user budgets (M2) |
| **Elevation of privilege** | Model output approves/executes/creates rules | No such tool exists (asserted by the injection suite); rules only via the authenticated UI | — |
| **Elevation of privilege** | Sandbox escape | Unprivileged, cap-drop ALL, read-only rootfs, no host mounts, internal network | Signed/pinned image (M5) |

## AI-specific threats
| Threat | Mitigation | Test |
|---|---|---|
| **Indirect prompt injection** (email/doc/web/MCP text tells the agent to act) | All retrieved text is labeled untrusted; writes are proposals only; destinations must come from the user's words or granted resources | `pytest -m injection` (run separately in CI) |
| **Injected destinations** (send to an attacker address, post to an ungranted channel) | Recipient/channel rails in code | `test_recipient_must_come_from_aj_or_thread`, Slack/home tier tests |
| **Memory poisoning** | `save_memory` only when the user's own message says "remember…" | `test_document_cannot_plant_a_memory` |
| **Tool poisoning / MCP rug pull** | Tools reviewed individually; definitions hash-pinned and re-checked every call | `test_rug_pull_disables_server` |
| **Exfiltration through tool arguments** (data smuggled in a URL) | Sandbox egress allowlist; network windows are T2 | sandbox network tests |
| **Excessive agency** | Registry allowlist, tiers, hard T2 floor, mission scopes and caps, Pause | registry invariant tests |
| **Confused deputy across users** (one user's agent reads another's data) | n/a while single-owner | Principal-scoped queries + isolation suite (M1) |

## Residual risks accepted for now (tracked as POA&M items)
- Content at rest is plaintext in SQLite (M2). The mitigation for now is OS full-disk encryption.
- A single owner password with no MFA (M1).
- Model prompts go to the commercial Anthropic API. **Do not load CUI** until M2 classification routing and a
  CUI-authorized provider path (Bedrock GovCloud or self-hosted) exist.
- The MCP streamable-HTTP client resolves DNS itself, so the pinning fix does not cover it yet (M2).
