# People, roles and sign-in

## Your data is yours
Each person on this JARVIS has their own conversations, memories, documents, plans, rules, connections and sandbox.
Nobody else sees them, admins included. Admins see account details (who exists, their role, when they last signed in)
and an activity log that shows what kind of thing happened and when, without the content.
To share a document with everyone, open Memory → Documents and choose **Share**. Others can read and search it but
can't change or delete it. Choose **Make private** to stop sharing.

## Signing in
- Sign in with your **username** and password, then the second step if you've set one up.
- **Second step:** an authenticator app (scan the QR code in Settings → Account) or a passkey (Face ID, Touch ID,
  Windows Hello or a security key). Owners, admins and approvers must have one; when JARVIS is reachable over the
  network, everyone must.
- **Recovery codes:** you get 10 when you add your first method. Each works once. Keep them somewhere safe. Settings →
  Account can make a new set, and the old set stops working.
- **Lost your phone?** Use a recovery code, or ask an admin to **Reset 2-step**. You'll set it up again at your next
  sign-in.
- **Forgot your password?** An admin sends a new invite link (Settings → People → New link).
- A phone paired with a code from Settings → Security & devices signs in as you.

## Roles
| Role | Can |
|---|---|
| Member | Use JARVIS for their own work: commands, their own connections, rules, missions and sandbox |
| Approver | Everything a member can, plus co-approve organization-wide changes |
| Admin | Everything an approver can, plus manage people (except owners and admins), org settings, budget, MCP servers, skills, the Telegram bot, workspace folders, backups, diagnostics, and the pause switch |
| Owner | Everything, including granting or removing the owner and admin roles |
| Auditor | Read the organization's activity log (who did what, when; no content). Can't give commands. |

## Inviting people (admins)
Settings → People → **Invite someone** creates the account and a single-use link that works for 72 hours. Send it to
the person; they choose their own password. Disabling someone signs them out everywhere and pauses their missions and
schedules; their data is kept. An account nobody has used for 90 days is disabled at its next sign-in attempt, and an
admin can re-enable it.

## Co-approval
In the business edition, some changes affect everyone, such as installing a skill. These need the requester's approval
**and** a second person's: an admin, owner or approver with the right permission. They wait in Actions →
**Needs your co-approval**.
