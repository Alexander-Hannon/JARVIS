# JARVIS V5 — "Overseer" roadmap

## Context
V4 made JARVIS a strong personal operator: registries, tiered autonomy, MCP, sandbox, missions. The next goal is commercial: a company runs its own internal AI overseer. It watches the whole organization, understands it, coordinates work, and acts within limits the company sets. Prospective customers will hold **CUI**, so security comes first.

AJ's decisions (2026-10-05):
- **Deployment:** per-customer installs. Each company gets its own isolated deployment: its own GovCloud or Azure Gov enclave, on-prem, or air-gapped. There is one org per database, so CUI never leaves the customer's boundary.
- **LLM paths:** Claude on Bedrock GovCloud (the CUI path), the Anthropic API (commercial and personal use), a self-hosted open model (air-gapped), and Vertex/Azure.
- **Personal edition:** same codebase. AJ's setup becomes a one-user "personal" org preset, and personal-only connectors (Canvas, Spotify, GTA, SimpleFIN, Home Assistant) sit behind an edition flag.
- **Order:** the security core comes first. Overseer powers build on it from M3.

The audit found that today's code is single-owner throughout:
- **Schema:** none of the ~55 tables has a `user_id` or `org_id`; there are about 52 `WHERE id=1` queries; "AJ" is hardcoded in prompts.
- **Auth:** password only, with no MFA.
- **Encryption:** only token columns are encrypted. The database, files and backups are plaintext.
- **Audit:** the audit log is mutable.
- **LLM:** the Anthropic public endpoint is hardwired, with no data classification.
- **Remote setup bypass:** behind the reverse proxy, every request looks like loopback (`proxy_headers=False`), so `is_loopback()` (`security/auth.py:111`) passes for anyone who reaches the proxy.

## V5 invariants (unchanged from V4, extended to many users)
- Model output never approves, executes or grants anything. Every write goes through `actions/executor.py` and the registry. Tiers T1/T2/T3 stay, plus the hard T2 floor.
- **Agents act as a person, never as the system.** Every run carries a `principal` (user, org and roles). Adapters use that user's delegated credentials and the source system's ACLs. There is no superuser agent.
- **New tier modifier `dual`:** org-impacting T2 actions need two different approvers. Examples: org-wide posts, permission changes, and policy rules that span users.
- **Data classification is enforced in code:** CUI-labelled content may only reach `cui_authorized` model providers, and server-side web tools are disabled for any run whose context contains CUI.
- Retrieved text is still untrusted data. It never grants permissions, picks destinations or approves.

---

## M0 — Security baseline and program (small, ships first)
Fix the audit findings and set up the security program:
- **Setup bypass.** Add a real console setup token: generated at `serve` start when no owner exists, printed once, single use. Add a `JARVIS_TRUSTED_PROXIES` setting and enable proxy headers only for those addresses. Remove `"testclient"` from loopback in non-test settings. Files: `security/auth.py`, `api/routes.py:55`, `cli.py`, `config.py`.
- **Sessions and login.**
  - Idle timeout and absolute timeout (800-171 3.1.10/3.1.11), with session rotation at login.
  - Per-account and per-IP lockout with backoff, stored in the DB (replaces the global in-memory throttle at `routes.py:67`).
  - Rate limit on `/auth/pair`.
  - Failed logins are audited with IP, user agent and session.
- **Headers.** A strict CSP for the SPA and HSTS when HTTPS. The `Secure` cookie follows the real scheme. File: `api/app.py:74`.
- **netguard.** Pin the resolved IP for the connection to close the DNS-rebinding gap (`security/netguard.py:30-47`), and route every outbound adapter fetch through it.
- **Secrets.** Encrypt the OAuth PKCE `code_verifier` in a new migration `0007_v5_security.sql`.
- **Supply chain / CI.** A GitHub Actions workflow that runs:
  - ruff, pytest, tsc, Playwright (mocked)
  - `pip-audit`, `npm audit`, `bandit`, `semgrep`
  - gitleaks secret scanning
  - SBOM (CycloneDX) generation
  - Dependabot
- **Security docs** (`docs/security/`):
  - `THREAT_MODEL.md` (STRIDE plus AI-specific risks: prompt injection, tool poisoning, data exfiltration through the model)
  - `NIST_800-171_MAPPING.md`: all 110 controls, marked *product-provided*, *customer-provided* or *shared*. This becomes the seed for the customer SSP.
  - `SECURITY.md` (vulnerability disclosure)
  - an incident-response runbook
- **Prompt-injection regression suite.** Group the existing injected-destination tests and add new ones covering email, docs, MCP output and the sandbox. Run it in CI.

## M1 — Identity, users, RBAC, Postgres
- **Schema.** Migration `0008_v5_identity.sql` adds:
  - `orgs` (one per deployment) and `users`
  - `roles`: `owner`, `admin`, `approver`, `member`, `auditor` (read-only, can see audit and logs), `service`
  - `memberships`
  - `user_id` on user-owned tables: sessions, conversations/messages/runs, memories, documents, proposals and approvals (`approved_by`), policies, integration_accounts, grants, missions, notifications, budgets
  - Singleton `user_settings` / `owner` rows are migrated to `user_settings(user_id)` plus `org_settings`. The existing DB becomes org 1, user 1 (AJ), and the migration is tested on a copy, as in V4.
- **Scoping.** A `Principal` dataclass is resolved by `require_session`, and every query is scoped through repository helpers. A test asserts that no remaining `WHERE id=1` touches user data. A cross-user isolation test suite tries every API route as user B against user A's data.
- **Authentication.**
  - OIDC SSO: Entra ID (commercial and GCC High endpoints), Okta and generic OIDC.
  - Local password plus MFA: WebAuthn/passkeys (phishing-resistant) and a TOTP fallback.
  - MFA is required for privileged roles (3.5.3).
  - SCIM 2.0 provisioning endpoint.
- **Authorization.** A permission matrix checked by a `require(perm)` dependency on every route. Approvers must hold the permission for the action's resource. Dual approval is implemented in `actions/proposals.py` and the executor re-checks it.
- **Database.**
  - Add a dialect layer in `core/db.py`. Postgres is the enterprise default; SQLite stays for the personal edition.
  - Port the SQLite-isms (~34 sites): `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`, `json_extract`, `BEGIN IMMEDIATE`, FTS5 → `tsvector` with an equivalent `bm25` ranking.
  - Job leases switch to `SELECT … FOR UPDATE SKIP LOCKED` so several workers can run.
  - The test suite runs against both databases.
- **Personalization.** The system prompt, location, persona ("sir") and seed come from user and org settings instead of hardcoded AJ/Huntsville (`agents/agent.py:50-110`, `core/seed.py`). Personal-only specialists and connectors are gated by `edition`.

## M2 — Data protection and the CUI boundary
- **Envelope encryption.**
  - A `KeyProvider` interface with backends: local file (personal edition), AWS KMS (GovCloud), Azure Key Vault (Gov), HashiCorp Vault.
  - Each data class has its own DEK, wrapped by the KEK. Keys rotate and data is re-wrapped.
  - Fernet is replaced with AES-256-GCM.
  - Content columns (memories, documents/segments, messages, transcripts, email-derived text) are encrypted at the field level. Search indexes are built from plaintext inside the trust boundary, and the DB volume is also encrypted.
- **FIPS mode.** `JARVIS_FIPS=1` requires a FIPS-validated OpenSSL provider at startup (checked by `doctor`), refuses non-approved algorithms, and is documented for RHEL/UBI FIPS images.
- **Classification.**
  - Documents, messages and memories carry `classification` (`public | internal | cui | cui_specified`) plus CUI category and dissemination markings.
  - Labels are set by connector default, by the user, or by a rule-based detector that only suggests and never downgrades.
  - Banners appear in the UI. Exports carry markings.
- **Classification-aware model routing.**
  - The `llm/provider.py` interface is split into `AnthropicProvider`, `BedrockProvider` (`AnthropicBedrock`), `VertexProvider`, `AzureProvider` and `OpenAICompatProvider` (vLLM/local).
  - Each provider declares capabilities (server web tools, computer use, thinking, caching) and an authorization level (`commercial | fedramp_moderate | fedramp_high | onprem`).
  - The router refuses to send CUI to an unauthorized provider. That is a hard code check with a test.
  - Agent features degrade gracefully when a capability is missing.
  - Per-org and per-user budgets replace the global ledger.
- **Tamper-evident audit.**
  - Each `audit_events` row stores `prev_hash` and `hash`, with periodic signed checkpoints.
  - The table is append-only: an enforcing trigger in Postgres, revoked UPDATE/DELETE rights for the app role.
  - Every event records user, session, IP and request ID.
  - Export to a SIEM over syslog/CEF and OpenTelemetry.
  - Retention policies and legal hold.
  - A "Verify audit chain" check in `doctor`.
- **Backups.** Encrypted with the KMS key, with a signed manifest (not a sidecar SHA). The restore drill runs in CI.
- **Logging.** Structured JSON logs with a content/PII redaction filter. CUI never goes into logs.

## M3 — Overseer core (the Stark layer)
- **Company Brain.**
  - A knowledge graph (entities: people, teams, projects, systems, customers, contracts, commitments; plus their relations), built from connectors.
  - Hybrid retrieval: Postgres FTS plus `pgvector` embeddings. Embeddings come from the same authorized provider path (Bedrock/local).
  - **Permission-aware retrieval:** every chunk keeps its source ACL, and search filters by the asking principal.
- **Signals → Situations.**
  - Watchers ingest events (mail, chat, tickets, repos, calendars, infra and security alerts).
  - Deterministic rules plus model triage turn them into *Situations*: deadline at risk, blocked project, unanswered customer, incident, compliance drift.
  - Each Situation has an owner, severity, evidence links and suggested actions, which go through proposals.
- **Overwatch HUD.**
  - A leadership view with org health, live situations, a commitments ledger ("who promised what, by when"), risks, and an agent activity feed.
  - A per-employee JARVIS, which is the current product scoped to that user.
  - A voice briefing for each role.
- **Agent crews.**
  - Department agents (Ops, Engineering, Finance, Compliance, SecOps), each with a `Scope`, a budget and an owner.
  - Orchestration works as plan → delegate → verify, with a supervisor agent that checks sub-agents' evidence before reporting.
  - All writes still go through proposals and tiers.
- **Compliance Copilot (commercial wedge for CUI customers).**
  - Continuous NIST 800-171 / CMMC L2 control monitoring against connected systems.
  - Evidence collection, SSP and POA&M drafting and tracking.
  - It runs JARVIS's own control mapping from M0, so it is dogfooded first.

## M4 — Enterprise connectors (ACL-synced, delegated auth)
- Microsoft 365 via Graph (commercial and GCC High): Outlook, Teams, SharePoint/OneDrive, Planner.
- Google Workspace, Slack Enterprise, Jira/Confluence, ServiceNow, GitHub Enterprise, Salesforce, Okta/Entra (directory), and AWS/Azure security findings.
- Each one follows the existing adapter pattern (Protocol, adapter, labelled fake, registry action types with tiers), plus ACL sync and a classification default.
- The MCP hub gets org-level review: an admin approves servers and tools for the org, and users can only enable what is approved.

## M5 — Deployment, operations and packaging
- **Containers.**
  - Hardened images on UBI/FIPS base, rootless and read-only, cosign-signed, with SBOM attached.
  - `docker compose` for small installs; a Helm chart for Kubernetes.
- **Infrastructure as code.** Terraform for AWS GovCloud: VPC with no public ingress, private Bedrock endpoint, RDS Postgres (encrypted), KMS, S3 with object lock for audit/backups.
- **Air-gapped bundle.** Images, a vLLM model and offline install docs.
- **Admin console.** Users, roles, SSO, keys, providers, retention, connector approvals, health, licence.
- **Assurance.** An external penetration test before the first CUI pilot, and backup/DR runbooks with RPO/RTO.
- **Product docs.** Customer SSP template and a shared-responsibility matrix.

> Compliance note: CMMC L2 is certified for the *customer's* environment by a C3PAO. JARVIS provides the controls and the evidence. If we ever *operate* an install in a customer enclave, our ops staff and processes come into scope too (US persons, training, IR), so that is decided per contract.

---

## How V5 ships
- Each milestone ships on its own: tests green, `BUILD_STATUS.md` updated, one PR into `main`.
- `CLAUDE.md` safety boundaries are rewritten in M1 (principals, dual approval) and M2 (classification routing).
- M1 and later each get a detailed sub-plan when they start; the M1 schema migration and Postgres port carry the most risk.
- No runtime API spend, cloud purchases or public deployment without AJ's explicit approval.

## Status
| Milestone | State |
|---|---|
| M0 Security baseline | **landed** (session 10, 2026-10-05) — see `BUILD_STATUS.md` |
| M1 Identity, RBAC, Postgres | next |
| M2 Data protection, CUI boundary | planned |
| M3 Overseer core | planned |
| M4 Enterprise connectors | planned |
| M5 Deployment and packaging | planned |
