# NIST SP 800-171 Rev. 2 → JARVIS control mapping

This is the working map of all 110 security requirements (the CMMC Level 2 baseline) for a per-customer JARVIS install.
It seeds the customer's System Security Plan (SSP). It does **not** claim certification: CMMC Level 2 is assessed on the
customer's environment by a C3PAO, and JARVIS is one component inside it.

- **Responsibility.** *Product*: JARVIS implements it. *Customer*: the organization's own program, people or
  infrastructure. *Shared*: both have a part.
- **Status** is the honest state of the code today:
  - *in place*: implemented and tested.
  - *partial*: some of it exists; the gap is named.
  - *planned Mx*: scheduled in `docs/V5_ROADMAP.md`.
  - *customer*: nothing for the product to do.
- **Review.** Every milestone updates this file. Each *planned* row is a product POA&M item.

**Summary (session 11, 2026-10-06):** 19 in place · 41 partial · 15 planned · 35 customer-only.

## 3.1 Access Control

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.1.1 | Limit access to authorized users, processes, devices | Shared | in place | M1a: named user accounts (`users`), per-user sessions and device pairing; every route resolves a `Principal`; disabled/dormant users are refused. Data is private per user: services filter on `user_id`, DB triggers refuse unscoped rows, foreign ids answer 404 (`tests/test_v5_isolation.py` sweeps every route). Customer decides who gets an account. |
| 3.1.2 | Limit access to permitted transactions and functions | Product | in place | M1a: role → permission matrix (`security/principal.py` `PERMS`) enforced per route (`require(perm)`), tested in `tests/test_v5_roles.py`. Executable writes are only the types in `actions/registry.py`; T2/T3 tiers. |
| 3.1.3 | Control the flow of CUI | Product | planned M2 | Classification labels + provider authorization routing (CUI only to `cui_authorized` providers). Sandbox egress allowlist and SSRF guard exist today. |
| 3.1.4 | Separation of duties | Shared | partial | M1a: `approver` and `auditor` roles; org-impacting actions with `co_approver_perm` (`skill.install`) need a second, different person (enterprise edition); only owners grant admin/owner. Gap: more action types to mark; the customer defines who holds which role. |
| 3.1.5 | Least privilege | Shared | partial | M1a: role-scoped permissions; agents, missions and channels act as the requesting user (their accounts, grants, sandbox only); per-resource read/write grants; agent `Scope`; sandbox cap-drop ALL. Customer assigns minimal roles. |
| 3.1.6 | Non-privileged accounts for nonsecurity functions | Shared | partial | M1a: `member` role has no org functions; admins can hold a separate member account. Customer policy to use it. |
| 3.1.7 | Prevent non-privileged users from executing privileged functions; audit it | Product | in place | M1a: user management, org settings, integrations (MCP, skills, Telegram bot, workspace roots, sandbox image), backups, diagnostics and org pause require admin permissions (403 otherwise); every change is audited with the actor's user id. |
| 3.1.8 | Limit unsuccessful logon attempts | Product | in place | Per-IP DB-backed throttles (login 10 / pairing 5 / setup 10 / invite 10 / MFA 10 per 15 min), per-username limit of 10 that still admits devices which signed in as that user, account-wide limit (`security/auth.py`). |
| 3.1.9 | Privacy and security notices | Shared | partial | First-run screen states data flows. M1: configurable login banner text for the customer's CUI notice. |
| 3.1.10 | Session lock after inactivity | Shared | in place | M0: idle timeout `JARVIS_SESSION_IDLE_HOURS` ends the session for good. Pattern-hiding screen lock is the endpoint's (customer). |
| 3.1.11 | Terminate session after a defined condition | Product | in place | M0: absolute lifetime (`JARVIS_SESSION_TTL_HOURS`) + idle timeout; logout revokes; password reset revokes all. |
| 3.1.12 | Monitor and control remote access sessions | Shared | partial | Sessions record IP + user agent (M0); Settings lists devices. Network-level remote access is the customer's (VPN/ZTNA). |
| 3.1.13 | Crypto for remote access confidentiality | Shared | partial | Remote profile requires an HTTPS front; HSTS + Secure cookies (M0). FIPS-validated TLS is the deployment's (M5 images). |
| 3.1.14 | Route remote access via managed access control points | Customer | customer | Loopback bind by default; exposure only through the customer's HTTPS front / VPN. |
| 3.1.15 | Authorize remote privileged commands | Product | partial | No host shell exists; sandbox shell is containerized and network-gated (T2). M1: admin functions require role + MFA. |
| 3.1.16 | Authorize wireless access | Customer | customer |  |
| 3.1.17 | Protect wireless access | Customer | customer |  |
| 3.1.18 | Control connection of mobile devices | Shared | partial | Phones join only via one-time pairing codes (10 min, rate-limited). MDM is the customer's. |
| 3.1.19 | Encrypt CUI on mobile devices | Customer | customer | PWA stores no CUI offline beyond the browser cache (`no-store` on all API responses). |
| 3.1.20 | Verify and control connections to external systems | Shared | partial | Connectors require explicit grants; MCP servers are hash-pinned and reviewed; SSRF guard pins DNS (M0). M4: org-level connector approval. |
| 3.1.21 | Limit portable storage on external systems | Customer | customer |  |
| 3.1.22 | Control CUI posted on publicly accessible systems | Shared | partial | Anything reaching others (email send, Slack post) is T2 + `reaches_others`. M2: block CUI-labeled content from such actions without a matching destination authorization. |

## 3.2 Awareness and Training

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.2.1 | Security awareness | Customer | customer | Help manual documents the safety model; training is the customer's program. |
| 3.2.2 | Role-based training | Customer | customer |  |
| 3.2.3 | Insider threat awareness | Customer | customer |  |

## 3.3 Audit and Accountability

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.3.1 | Create and retain audit records | Shared | partial | `audit_events` records auth, approvals, policy, connector and execution events. M2: retention policy, legal hold, SIEM export. |
| 3.3.2 | Trace actions to individual users | Product | in place | M1a: `actor_user_id` on every audit event, `approved_by` on every approval, `user_id` on proposals/runs/actions; auth events carry IP + user agent. M2 adds request ids. |
| 3.3.3 | Review and update logged events | Shared | planned M2 | Event catalog documented; customer reviews it annually. |
| 3.3.4 | Alert on audit logging failure | Product | planned M2 | Doctor check + alert when the audit chain/export fails. |
| 3.3.5 | Correlate audit review, analysis, reporting | Shared | planned M2 | Syslog/CEF + OpenTelemetry export to the customer's SIEM. |
| 3.3.6 | Audit reduction and report generation | Product | partial | Activity page filters by actor; M2 adds exports. |
| 3.3.7 | Authoritative time source | Customer | customer | All instants are UTC from the host clock; the host must use NTP. |
| 3.3.8 | Protect audit information | Product | planned M2 | Hash-chained, append-only audit with signed checkpoints; Postgres role cannot UPDATE/DELETE. |
| 3.3.9 | Limit audit management to privileged users | Product | partial | M1a: org-wide audit view needs `audit.read` (owner/admin/auditor) and shows other people's events as metadata only; no API or agent tool edits or deletes audit rows. Gap: tamper-evident export (M2). |

## 3.4 Configuration Management

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.4.1 | Baseline configurations and inventories | Shared | partial | Locked dependencies (`uv.lock`, `package-lock.json`); CycloneDX SBOM in CI (M0). M5: signed images. |
| 3.4.2 | Security configuration settings | Shared | partial | Secure defaults (loopback, HTTPS-only remote, CSP). `jarvis doctor` reports deviations (M0). |
| 3.4.3 | Track, review, approve changes | Shared | partial | PR review + CI gates (M0) for the product; customer change control for their install. |
| 3.4.4 | Security impact analysis of changes | Shared | partial | Threat model updated per milestone; customer analyzes upgrades. |
| 3.4.5 | Access restrictions for change | Shared | partial | Policy rules/connectors only via authenticated UI; M1 restricts to admins. |
| 3.4.6 | Least functionality | Product | in place | Integrations, MCP tools and sandbox are off until enabled; MCP tools individually reviewed. |
| 3.4.7 | Restrict nonessential programs/ports/services | Shared | partial | Single port, loopback default, no inbound ports for channels (outbound polling). |
| 3.4.8 | Application allow/deny listing | Shared | partial | Skills only from local folders or approved drafts; MCP stdio commands reviewed. Host allowlisting is the customer's. |
| 3.4.9 | Control user-installed software | Shared | partial | Skills/MCP install gated by approval; M4 moves it to admins. |

## 3.5 Identification and Authentication

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.5.1 | Identify users, processes, devices | Product | partial | M1a: unique user accounts; sessions labeled per device and bound to a user; system actors (`worker`, `policy`, `mission`) are named. Gap: service identities for API clients (M1b). |
| 3.5.2 | Authenticate users, processes, devices | Product | partial | scrypt password + second factor, pairing codes issued by an authenticated session to that same user. M1b: OIDC SSO (Entra ID incl. GCC High, Okta). |
| 3.5.3 | Multifactor authentication | Product | in place | M1a: TOTP (RFC 6238) and WebAuthn passkeys, 10 single-use recovery codes (`security/mfa.py`). `JARVIS_MFA_POLICY=auto` requires it for every account in the remote profile (network access) and for owner/admin/approver locally; a session owing MFA reaches only enrollment. SSO installs will inherit the IdP's MFA (M1b). |
| 3.5.4 | Replay-resistant authentication | Product | in place | M1a: WebAuthn challenges are single use; TOTP steps can't be reused (`last_step`); MFA challenges expire in 5 min; random session tokens over TLS; CSRF tokens. |
| 3.5.5 | Prevent reuse of identifiers | Shared | partial | M1a: user ids are random and never reused; disabled accounts keep their username. Gap: SCIM deprovisioning (M1b). |
| 3.5.6 | Disable identifiers after inactivity | Product | in place | M1a: an account unused for `JARVIS_DORMANT_DAYS` (default 90) is disabled at its next sign-in attempt (owners are exempt so an install can't lock itself out); an admin re-enables it. SSO installs will also rely on the IdP. |
| 3.5.7 | Password complexity | Product | partial | 12-character minimum (M0). M1: breached-password check; SSO preferred. |
| 3.5.8 | Prohibit password reuse | Product | planned M1 |  |
| 3.5.9 | Temporary passwords | Product | in place | M1a: no temporary passwords — single-use invite links (72 h, stored hashed) where the person sets their own; re-issuing voids the old link. |
| 3.5.10 | Store/transmit only protected passwords | Product | in place | scrypt hashes; TLS in remote profile; tokens stored as SHA-256 hashes. |
| 3.5.11 | Obscure authentication feedback | Product | in place | Password inputs masked; one generic 'Invalid credentials' message. |

## 3.6 Incident Response

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.6.1 | Incident handling capability | Shared | partial | `docs/security/INCIDENT_RESPONSE.md` (M0) covers product-side containment (pause, revoke, rotate). |
| 3.6.2 | Track, document, report incidents | Customer | customer | Includes DFARS 252.204-7012 72-hour reporting. |
| 3.6.3 | Test incident response | Customer | customer | Restore drill in CI planned (M2). |

## 3.7 Maintenance

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.7.1 | Perform maintenance | Customer | customer |  |
| 3.7.2 | Control maintenance tools | Customer | customer |  |
| 3.7.3 | Sanitize equipment for off-site maintenance | Customer | customer |  |
| 3.7.4 | Check diagnostic media for malicious code | Customer | customer |  |
| 3.7.5 | MFA for nonlocal maintenance | Shared | planned M1 | Admin console requires MFA. |
| 3.7.6 | Supervise maintenance personnel | Customer | customer |  |

## 3.8 Media Protection

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.8.1 | Protect system media | Customer | customer |  |
| 3.8.2 | Limit access to CUI on media | Customer | customer |  |
| 3.8.3 | Sanitize media before disposal | Shared | partial | Forget/delete scrubs content + FTS; M2 crypto-shredding by destroying per-class keys. |
| 3.8.4 | Mark media with CUI markings | Product | planned M2 | Exports and backups carry classification markings. |
| 3.8.5 | Control media during transport | Customer | customer |  |
| 3.8.6 | Crypto for CUI on digital media in transport | Product | planned M2 | Backups encrypted with the customer's KMS key. |
| 3.8.7 | Control removable media | Customer | customer |  |
| 3.8.8 | Prohibit unowned portable storage | Customer | customer |  |
| 3.8.9 | Protect backup CUI | Product | planned M2 | Today backups exclude the key but hold content in plaintext; M2 encrypts + signs them. |

## 3.9 Personnel Security

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.9.1 | Screen individuals | Customer | customer | If we operate an install, our staff are in scope too. |
| 3.9.2 | Protect CUI during personnel actions | Shared | planned M1 | SCIM deprovisioning revokes sessions and delegated connector tokens. |

## 3.10 Physical Protection

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.10.1 | Limit physical access | Customer | customer |  |
| 3.10.2 | Protect and monitor facility | Customer | customer |  |
| 3.10.3 | Escort visitors | Customer | customer |  |
| 3.10.4 | Physical access logs | Customer | customer |  |
| 3.10.5 | Manage physical access devices | Customer | customer |  |
| 3.10.6 | Safeguard CUI at alternate work sites | Customer | customer |  |

## 3.11 Risk Assessment

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.11.1 | Periodic risk assessment | Shared | partial | `docs/security/THREAT_MODEL.md` (M0), updated per milestone. |
| 3.11.2 | Vulnerability scanning | Shared | in place | CI: pip-audit, npm audit, bandit, semgrep, gitleaks on every PR (M0). Host scanning is the customer's. |
| 3.11.3 | Remediate vulnerabilities | Shared | partial | Dependabot weekly (M0); `SECURITY.md` disclosure process. |

## 3.12 Security Assessment

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.12.1 | Periodically assess controls | Shared | partial | This mapping, reviewed each milestone; external pentest before the first CUI pilot (M5). |
| 3.12.2 | Plans of action (POA&M) | Shared | partial | Every 'planned' row here is a product POA&M item; M3 Compliance Copilot tracks the customer's. |
| 3.12.3 | Monitor controls continuously | Shared | partial | `jarvis doctor` security checks (M0); M3 continuous monitoring. |
| 3.12.4 | System security plan | Customer | customer | This file seeds the customer SSP (M5 template). |

## 3.13 System and Communications Protection

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.13.1 | Monitor/control communications at boundaries | Shared | partial | Host/Origin allowlists, loopback bind, sandbox `--internal` network + egress proxy. |
| 3.13.2 | Secure architecture and engineering | Product | in place | Registry-only writes, executor guards, untrusted-data labeling, approvals bound to version+hash (see `CLAUDE.md`). |
| 3.13.3 | Separate user functionality from management | Product | planned M1 | Admin console and roles. |
| 3.13.4 | Prevent info transfer via shared resources | Product | partial | Sandbox is disposable with no host mounts. M1: per-user data isolation tests. |
| 3.13.5 | Subnetworks for public components | Customer | customer | No public component by design. |
| 3.13.6 | Deny network traffic by default | Shared | partial | Sandbox egress denied by default; outbound adapters only to configured providers. |
| 3.13.7 | Prevent split tunneling | Customer | customer |  |
| 3.13.8 | Crypto in transmission | Shared | partial | HTTPS front required remotely; HSTS (M0); providers over TLS with hostname verification (DNS-pinned for user URLs). |
| 3.13.9 | Terminate connections at session end/inactivity | Product | in place | Idle + absolute session limits (M0); streams capped (`STREAM_MAX_SECONDS`). |
| 3.13.10 | Establish and manage crypto keys | Product | planned M2 | Today: one local Fernet key file outside the DB. M2: KMS/Key Vault/Vault envelope keys with rotation. |
| 3.13.11 | FIPS-validated cryptography | Shared | planned M2 | FIPS mode requiring a validated OpenSSL provider; UBI FIPS images (M5). |
| 3.13.12 | Collaborative computing devices | Shared | partial | Microphone only on explicit push-to-talk; Permissions-Policy denies camera/geolocation; no remote activation. |
| 3.13.13 | Control mobile code | Product | in place | CSP `script-src 'self'`, no inline or third-party script (M0); sandboxed browser for web content. |
| 3.13.14 | Control VoIP | Customer | customer |  |
| 3.13.15 | Protect session authenticity | Product | in place | HttpOnly SameSite=Strict cookies, CSRF tokens, session rotation on login (M0), Host/Origin checks. |
| 3.13.16 | Protect CUI at rest | Shared | planned M2 | Today only credentials are encrypted. M2: field-level envelope encryption of content + encrypted volumes. |

## 3.14 System and Information Integrity

| Req | Requirement | Responsibility | Status | How / gap |
|---|---|---|---|---|
| 3.14.1 | Identify and correct flaws | Shared | partial | CI scanners + Dependabot (M0). |
| 3.14.2 | Malicious code protection | Customer | customer | Uploads are parsed, never executed; code runs only in the sandbox. |
| 3.14.3 | Monitor security alerts and advisories | Shared | partial | Dependabot/GitHub advisories for the product. |
| 3.14.4 | Update malicious code protection | Customer | customer |  |
| 3.14.5 | Periodic and real-time scans | Customer | customer |  |
| 3.14.6 | Monitor for attacks and indicators | Shared | partial | Failed sign-in audit with IP (M0) + doctor alert; M2 SIEM export. |
| 3.14.7 | Identify unauthorized use | Shared | partial | Activity log; M3 anomaly signals on agent/action activity. |
