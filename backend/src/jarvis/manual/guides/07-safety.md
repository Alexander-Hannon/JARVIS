# Safety model
- Approvals are bound to the exact plan you saw. Editing a plan voids its approval.
- Text JARVIS reads (email, web pages, documents, Slack, MCP results) is treated as data, never as instructions. It can't
  grant permissions, pick recipients or approve anything.
- Recipients and destinations must come from your words, the thread you asked to reply to, or targets you allowed.
- If a write times out, JARVIS checks whether it landed before trying again. It never sends an email twice.
- Secrets are stored encrypted and never shown to the model.
- JARVIS listens only on this machine unless you set up remote access yourself.
