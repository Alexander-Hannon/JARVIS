# Troubleshooting
- **"Not connected":** open Connections and connect or reconnect the app. JARVIS says "not connected" rather than
  pretending there's nothing there.
- **Nothing happens after approval:** the background worker must be running (`uv run jarvis worker`); the top bar shows
  its status.
- **Model unavailable:** Settings → Budget; check the key and the daily/monthly limits. Offline mode still handles tasks,
  calendar and approvals.
- **Sandbox unavailable:** enable Docker Desktop's WSL integration, then Workbench → Build.
- **Diagnostics:** Settings → Diagnostics runs the doctor checks.
