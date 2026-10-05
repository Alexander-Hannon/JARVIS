# Deployment profiles

Running the code and installing an always-on service are different things. Nothing here is deployed automatically. Nothing is purchased, published or exposed through router ports.

## 1. Local desktop (default, free)
- `jarvis serve` binds to **127.0.0.1:8765** only. `jarvis worker` runs beside it.
- Host-header checks (DNS-rebinding defense), origin checks, CSRF tokens and SameSite=Strict cookies are always on.
- **Limit:** when the laptop sleeps or is offline, nothing runs.
  - Scheduled briefings are missed.
  - On wake, the worker handles missed runs once:
    - Only the most recent missed run within its window (2–4 h) runs, labeled "catch-up".
    - Older runs are marked missed, with at most one notice. You never get a notification storm.
- WSL2: the worker only runs while WSL is running. To keep it alive, keep a WSL terminal open, or use a Windows scheduled task at logon: `wsl.exe -d Ubuntu -- bash -lc "cd ~/dev/JARVIS/backend && uv run jarvis worker"`.

## 2. Always-on worker on a host you control (optional)
Use this for briefings at 6:30 AM while your laptop sleeps.
- **Host options:**
  - A home mini-PC or desktop that stays on: $0 beyond hardware and power.
  - A small VPS: typically ~$5–10/month. This is a cost decision for you to make; Jarvis will not subscribe to anything.
- **systemd user services** (Linux):
  ```ini
  # ~/.config/systemd/user/jarvis-worker.service
  [Unit]
  Description=JARVIS worker
  [Service]
  WorkingDirectory=%h/JARVIS/backend
  ExecStart=%h/.local/bin/uv run jarvis worker
  Restart=on-failure
  [Install]
  WantedBy=default.target
  ```
  Create a matching `jarvis-serve.service` with `ExecStart=… jarvis serve`. Then run `systemctl --user enable --now jarvis-worker jarvis-serve` and `loginctl enable-linger $USER`.
- **Host requirements:**
  - Copy `.env` and `data/secret.key` with 0600 permissions.
  - Keep full-disk encryption on. Only the token columns are encrypted; the SQLite file itself is not.
- The Settings page shows the worker heartbeat and each schedule's last success.

## 3. Phone access (authenticated HTTPS; your choice of private access method)
The phone **cannot** reach your desktop's `localhost`. Pick one of these:
- **Tailscale (recommended, free for personal use):** install it on the host and the phone, then run `tailscale serve --https=443 http://127.0.0.1:8765`. This gives a private `https://<host>.<tailnet>.ts.net` that only your devices can reach.
- **Your own reverse proxy** (Caddy or nginx) with TLS on a private network or VPN.

Then set these in `.env`:
```
JARVIS_REMOTE_MODE=true                         # secure cookies; required for any non-loopback origin
JARVIS_PUBLIC_ORIGIN=https://jarvis.<tailnet>.ts.net
JARVIS_ALLOWED_HOSTS=["127.0.0.1","localhost","jarvis.<tailnet>.ts.net"]
JARVIS_TRUSTED_PROXIES=["127.0.0.1"]            # believe X-Forwarded-For from the local HTTPS front (per-device throttles + audit IPs)
```
- **First-run setup through the HTTPS front:** every proxied request comes from 127.0.0.1, so it never counts as "this
  machine". Setup asks for the one-time token that `jarvis serve` prints on its console, or that `jarvis setup-token`
  prints. Remote mode always requires the token.
- **Sessions:** a session ends after 7 days unused (`JARVIS_SESSION_IDLE_HOURS`) or 14 days in total
  (`JARVIS_SESSION_TTL_HOURS`). Settings → Security & devices lists every signed-in device with its address and can
  sign any of them out.
Keep `jarvis serve` bound to 127.0.0.1 and let the HTTPS front forward to it. Pair the phone from the desktop: Settings → Pair device shows an 8-character, 10-minute code; on the phone choose "Pair with a code". For Google OAuth over this origin, add `https://jarvis.<tailnet>.ts.net/api/integrations/google/callback` to the OAuth client's redirect URIs.

**iPhone specifics:**
- Test these manually; they are not verified by automated tests.
- Microphone capture needs HTTPS. Safari records `audio/mp4`, and Jarvis's server-side decoding of that format is verified.
- Web Push only works for a web app **added to the Home Screen** (iOS 16.4+), with notifications allowed from inside that installed app.
- Background wake-word listening is not possible. Hands-free mode is a foreground session with a time limit.

## Web Push
Run `uv run jarvis gen-vapid`, put the keys in `.env`, restart, then enable notifications in Settings.

Delivery states are reported as:
- **generated:** in-app notification row created.
- **queued:** a push job was created.
- **accepted:** the push service returned success. This is not proof that you saw it.
- **opened:** you tapped the notification.

Lock-screen text is minimal by default.

## Containers
Not provided on purpose. A container would hide the microphone (browser), filesystem roots, OAuth redirect and network prerequisites this app depends on. If you want one later, bind-mount `data/` and `.env`, and keep loopback plus HTTPS-front semantics.
