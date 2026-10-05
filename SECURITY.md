# Security policy

## Reporting a vulnerability
Please report vulnerabilities privately through **GitHub → Security → Report a vulnerability** (private advisory) on
this repository. Do not open a public issue. Include:
- affected version or commit
- reproduction steps
- impact

Prompt-injection paths that lead to an action without approval count as vulnerabilities.

What to expect:
- acknowledgement within 3 business days
- an initial assessment within 10 business days
- coordinated disclosure once a fix is available

## Scope
In scope:
- the JARVIS backend and frontend in this repository
- the sandbox image and egress proxy (`backend/sandbox/`)
- the CI configuration

Out of scope:
- third-party providers
- a customer's own infrastructure

## How JARVIS is built to be safe
- **Runtime boundaries:** `CLAUDE.md`
- **Threat model:** `docs/security/THREAT_MODEL.md`
- **NIST SP 800-171 mapping:** `docs/security/NIST_800-171_MAPPING.md`
- **Incident response:** `docs/security/INCIDENT_RESPONSE.md`
