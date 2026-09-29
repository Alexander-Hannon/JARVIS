# JARVIS — Step-by-Step Guide

This takes you from a fresh clone to talking to Jarvis every day. Work through the parts in order; each step ends with a **✅ Check** so you know it worked before moving on.

> **Time needed:** about 20 minutes for Parts 1–4 (Jarvis running with voice). Connecting accounts (Part 5) takes another 20–30 minutes, mostly for Google's one-time setup.

---

## Contents

1. [Before you start](#part-1--before-you-start)
2. [Install Jarvis](#part-2--install-jarvis)
3. [Start Jarvis](#part-3--start-jarvis)
4. [First-run setup in the browser](#part-4--first-run-setup-in-the-browser)
5. [Connect your accounts](#part-5--connect-your-accounts)
6. [Turn on briefings and notifications](#part-6--turn-on-briefings-and-notifications)
7. [Optional: model budget, workspace folder, phone](#part-7--optional-extras)
8. [Using Jarvis day to day](#part-8--using-jarvis-day-to-day)
9. [Safety controls you should know](#part-9--safety-controls-you-should-know)
10. [Backups](#part-10--backups)
11. [Troubleshooting](#part-11--troubleshooting)
12. [Quick reference card](#quick-reference-card)

---

## Part 1 — Before you start

Jarvis runs on **your own computer**. On Windows, it runs inside **WSL2** (Ubuntu), and you use it from a normal Windows browser.

### What you need

| Item | Why | How to check |
|---|---|---|
| Windows 10/11 with **WSL2 + Ubuntu** | Runs Jarvis | In PowerShell: `wsl -l -v` shows Ubuntu, VERSION 2 |
| **Python 3.12** + **uv** | Backend | In Ubuntu: `python3 --version`, `uv --version` |
| **Node.js 20+** + npm | Builds the web UI | `node --version` |
| **git** | Get the code | `git --version` |
| ~1 GB free disk | Code + speech model (~142 MB) | — |
| Chrome or Edge | Microphone + UI | — |

### Install anything missing (inside Ubuntu)

```bash
# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node.js via nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc && nvm install --lts
```

✅ **Check:** `uv --version` and `node --version` both print a version.

> **macOS / Linux:** the same steps work in a normal terminal; skip everything that mentions WSL.

---

## Part 2 — Install Jarvis

Run everything below **in the Ubuntu (WSL) terminal**.

### Step 2.1 — Get the code

```bash
mkdir -p ~/dev && cd ~/dev
git clone https://github.com/Alexander-Hannon/JARVIS.git
cd JARVIS
```

(If you already have the folder, just `cd ~/dev/JARVIS && git pull`.)

### Step 2.2 — Install the backend

```bash
cd ~/dev/JARVIS/backend
uv sync
```

✅ **Check:** it ends without errors and `uv run jarvis --help` lists commands like `serve`, `worker`, `doctor`.

### Step 2.3 — Build the web interface

```bash
cd ~/dev/JARVIS/frontend
npm ci
npm run build
```

✅ **Check:** the last line says `✓ built in …`.

### Step 2.4 — Create your private settings file

```bash
cd ~/dev/JARVIS
cp .env.example .env
chmod 600 .env
```

You don't need to edit it yet — the defaults keep Jarvis private to your machine. You'll add keys here in Part 5.

> 🔒 `.env` holds secrets. It is already ignored by git — never commit it, paste it into chats, or share screenshots of it.

### Step 2.5 — Create the encryption key

```bash
cd ~/dev/JARVIS/backend
uv run jarvis setup-key
```

This creates `backend/data/secret.key`, which encrypts your stored tokens.

> ⚠️ **Back this file up somewhere safe** (e.g. a password manager's secure note or an encrypted drive). Without it, restored backups can't read your saved account tokens and you'd need to reconnect them.

### Step 2.6 — Download the speech model

```bash
uv run jarvis stt-warmup
```

The first run downloads about **142 MB** and takes a minute or two. It prints your hardware info and a measured warm-up time.

✅ **Check:** output includes `"state": "ready"`.

---

## Part 3 — Start Jarvis

Jarvis has **two processes**. Open **two Ubuntu terminals**.

**Terminal 1 — the app (web UI + API):**
```bash
cd ~/dev/JARVIS/backend
uv run jarvis serve
```

**Terminal 2 — the worker (runs approved actions, scheduled briefings, syncs):**
```bash
cd ~/dev/JARVIS/backend
uv run jarvis worker
```

Leave both running while you use Jarvis.

> 💡 The **worker** is what executes things you approve and generates scheduled briefings. If it isn't running, approvals will sit in "queued" and briefings won't arrive.

✅ **Check (optional, third terminal):**
```bash
cd ~/dev/JARVIS/backend && uv run jarvis doctor
```
You should see ✔ for Database, Token encryption key, Speech-to-text, and Worker. ⚠ items for accounts are expected until Part 5.

---

## Part 4 — First-run setup in the browser

### Step 4.1 — Open Jarvis

In **Chrome or Edge on the same computer**, go to:

```
http://localhost:8765
```

### Step 4.2 — Create your owner password

You'll see **"Set up your owner password."** Choose a password of **12+ characters** and click **Create owner account**.

> This only works from this computer — nobody on your network can do it for you. There is no default password.

### Step 4.3 — Allow the microphone

1. Click **Command** in the left sidebar.
2. Click the big **Tap or hold to talk** button.
3. When the browser asks for microphone access, click **Allow**.

### Step 4.4 — Try your first voice command

1. Tap the record button and say:
   > *"I need to grade IS 640 homework tomorrow, follow up with the iFlex lead Thursday, and buy groceries tonight."*
2. Tap **Stop & transcribe**.
3. Your words appear in an **editable box**. Fix anything that's wrong, then click **Send as command**.

✅ **Check:** Jarvis shows **3 tasks ready for Todoist** with the right dates, plus a note that **Todoist isn't connected yet** — that's expected. Nothing has been written anywhere.

> Typing works everywhere too: use the text box under the microphone.

### Step 4.5 — Pick a voice for spoken replies

Go to **Settings → Voice → Speech output**, choose a voice, and adjust the speed. You can mute Jarvis any time with **Mute voice** on the Command page.

---

## Part 5 — Connect your accounts

All of this lives in **Settings → Integrations**. Connecting an account only lets Jarvis **read** what you select. **Every change still needs your approval.**

### 5A — Todoist (≈3 minutes)

1. In Todoist (web): **Settings → Integrations → Developer**, copy your **API token**.
2. In Jarvis: **Settings → Integrations → Todoist**, paste the token, click **Connect**.
3. Choose the **project Jarvis may add tasks to**.
   - 💡 Recommended to start: create a Todoist project named **"Jarvis Test"** and select it. Switch to your real project once you trust it.
4. (Optional) Map areas like *GTA*, *School*, *Business* to specific Todoist projects.

✅ **Check:** Todoist shows **Connected**, and Today shows your due tasks.

### 5B — Google Calendar (≈15 minutes, one time)

Google requires you to create your own small "OAuth app" so Jarvis can connect. This is free.

**Part 1 — In Google Cloud Console**

1. Go to <https://console.cloud.google.com/> and sign in with the Google account whose calendar you use.
2. Top bar → project picker → **New Project** → name it `Jarvis` → **Create**.
3. Left menu → **APIs & Services → Library** → search **Google Calendar API** → **Enable**.
   (Also enable **Google Drive API** if you want Drive.)
4. **APIs & Services → OAuth consent screen**:
   - User type: **External** → Create.
   - App name: `Jarvis`; support email: yours.
   - Scopes: you can skip adding them here.
   - **Test users → Add users → your own Gmail address.** (Required while the app is in Testing mode.)
5. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**
   - Name: `Jarvis local`
   - **Authorized redirect URIs → Add URI:**
     ```
     http://localhost:8765/api/integrations/google/callback
     ```
     (It must match exactly. Jarvis also shows this under *Google Calendar & Drive* setup.)
   - Click **Create** and copy the **Client ID** and **Client secret**.

**Part 2 — Tell Jarvis**

6. Open `~/dev/JARVIS/.env` and add:
   ```
   JARVIS_GOOGLE_CLIENT_ID=paste-client-id-here
   JARVIS_GOOGLE_CLIENT_SECRET=paste-client-secret-here
   ```
7. Restart the app: in Terminal 1 press **Ctrl+C**, then run `uv run jarvis serve` again.

**Part 3 — Connect**

8. **Settings → Integrations → Google Calendar & Drive → Connect Google Calendar.**
9. Google may warn *"Google hasn't verified this app"* — that's normal for your own test app. Click **Continue**, then approve the permissions.
10. Back in Jarvis, choose:
    - **Calendars to read** (tick every calendar that affects your availability).
    - **One write calendar** — where Jarvis puts time blocks.
      💡 Recommended: in Google Calendar create a new calendar named **"Jarvis Test"** and select that.

> Jarvis will only ever write to a calendar **you own**, never to shared or other people's calendars, and never sends invitations.

✅ **Check:** Today shows your agenda and open work windows.

### 5C — Google Drive (optional)

1. **Connect Google Drive** (same screen). Approve.
2. Add the specific **folders or files** Jarvis may read. It never indexes your whole Drive and never edits Drive.

### 5D — Course deadline feed (≈3 minutes)

1. In Canvas: **Calendar → Calendar Feed** (bottom right) → copy the private link.
2. **Settings → Integrations → Course deadline feed (ICS)** → paste → **Connect feed**.
3. Review **course mappings** and confirm each course as **Own course** or **GTA**:
   - Own: MKT 601, Engineering Management Theory, Managerial Economics & Tech
   - GTA: IS 640, MSC 550, IS 401/501

> 🔒 The feed link is secret. Jarvis stores it encrypted and never displays it again.

✅ **Check:** Today → **Deadlines** lists upcoming items, and the source badge says **OK**.

### 5E — GitHub (optional)

Create a **fine-grained personal access token** (GitHub → Settings → Developer settings) with **read-only** access to *Metadata, Issues, Pull requests* for the repos you choose, then paste it with those repo names. Jarvis never pushes, merges, or creates issues.

---

## Part 6 — Turn on briefings and notifications

### Step 6.1 — Enable schedules

**Settings → Schedules.** The defaults are shown but **off** until you turn them on:

| Briefing | Time (Central) | What you get |
|---|---|---|
| **Daily Command** | 6:30 AM daily | Mission, top 3 priorities, agenda, due items, obstacle, first action |
| **Nightly Debrief** | 8:30 PM daily | What got done, what's open, questions, tomorrow's first action |
| **Weekly War Room** | Sunday 7:00 PM | Overdue patterns, next week's load, decisions |

Toggle on the ones you want. (Your separate 5:30 AM "Commit" challenge reminder is unaffected — Jarvis doesn't create or change it.)

> ⚠️ Briefings only run **while your computer is awake and the worker is running.** If the laptop was asleep, Jarvis runs at most **one** labeled catch-up when it wakes — never a flood of old notifications.

### Step 6.2 — Notifications

- Every briefing and action result lands in the **bell** (top right) automatically.
- Browser pop-up / push notifications (optional):
  1. In Ubuntu: `cd ~/dev/JARVIS/backend && uv run jarvis gen-vapid`
  2. Paste the two printed lines into `.env`, restart `jarvis serve`.
  3. **Settings → Notifications** → enable.

### Step 6.3 — Try a briefing now

Say or type **"brief me"**, or go to **Briefings → Generate → Daily Command**.

---

## Part 7 — Optional extras

### 7A — Smart answers (Anthropic model)

Jarvis works without a model: capture, tasks, corrections, scheduling, briefings, search, and memory all run locally. A model adds open-ended answers, screenshot/diagram reading, and richer meeting debriefs.

1. Get an API key at <https://console.anthropic.com/> (this is separate from any Claude chat subscription).
2. **Settings → Integrations → Anthropic** → paste key.
3. **Settings → Budget:**
   - Set a **daily limit** (e.g. $1.00) and optionally a monthly limit — required before enabling.
   - Turn on **Paid inference**.
   - Leave **background (scheduled) paid calls** off unless you want model-polished briefings.

> If you hit a limit, only model calls pause. Capture, tasks, approvals, and drafts keep working.

### 7B — A workspace folder for notes and exports

1. **Settings → Local files → Local workspace roots.**
2. Enter a **specific folder**, e.g. `/mnt/c/Users/<you>/Documents/Jarvis` (a Windows folder seen from WSL), give it a label, choose **read-only** or **writable**.
3. Then you can say things like *"create a note called Rubric ideas: …"* or *"save today's briefing as markdown."* Every write shows a preview and waits for approval.

> Jarvis refuses your whole home folder, hidden/config folders, and credential files, and can't follow shortcuts out of the folder.

### 7C — Use Jarvis on your iPhone

Your phone can't reach `localhost` on your laptop, so you need a private HTTPS link. The simplest free way is **Tailscale**:

1. Install Tailscale on your computer (Windows app) and iPhone; sign in to both with the same account.
2. In Ubuntu: `tailscale serve --https=443 http://127.0.0.1:8765` (see `DEPLOYMENT.md` for details), note your `https://<name>.<tailnet>.ts.net` address.
3. Add to `.env`, then restart `jarvis serve`:
   ```
   JARVIS_REMOTE_MODE=true
   JARVIS_PUBLIC_ORIGIN=https://<name>.<tailnet>.ts.net
   JARVIS_ALLOWED_HOSTS=["127.0.0.1","localhost","<name>.<tailnet>.ts.net"]
   ```
4. On the **computer**: **Settings → Security → Pair a phone** → note the 8-character code (valid 10 minutes).
5. On the **iPhone** (Safari): open your `https://…ts.net` address → **Pair with a code instead** → enter it.
6. Tap **Share → Add to Home Screen**, then open Jarvis from the home-screen icon (needed for push notifications on iPhone).

---

## Part 8 — Using Jarvis day to day

### Talking to Jarvis

- **Tap** the mic to start and tap again to stop, or **hold** to talk and release.
- Check the transcript, fix anything, then **Send as command**.
- Or just type.

### Things you can say

| You say | Jarvis does |
|---|---|
| "I need to grade IS 640 homework tomorrow and buy groceries tonight" | Splits into tasks with dates → proposal |
| "Change Thursday to Friday" / "make the second one Friday" | Updates the plan (new version) |
| "Remove the groceries one" | Drops that item |
| "Approve" / "approve 1 and 3" | Approves the plan it just showed you |
| "Cancel that" | Cancels the pending plan (nothing written) |
| "What did you just add?" | Shows what was done, with proof from Todoist/Google |
| "Block 45 minutes tomorrow afternoon for MKT 601 reading" | Finds a free slot → calendar proposal |
| "Mark grade MSC 550 quizzes done" | Proposes completing that Todoist task |
| "What should I do during my GTA shift?" | GTA duties first, then a conditional school queue |
| "Brief me" / "nightly debrief" / "war room" | Generates that briefing |
| "Remember that I do schoolwork during GTA shifts only after grading" | Saves/updates a preference |
| "Search my notes for rubric" | Answers from your uploaded sources, with citations |
| "Debrief this meeting" (with notes attached) | Decisions, your commitments, open owners, draft follow-ups |
| "How is Monk Mode going?" | Summary from your records (nothing invented) |

### How approvals work

1. Jarvis shows a **proposal card** with each item, the exact dates, which account/project/calendar it touches, and its assumptions.
2. Tick the items you want → **Approve selected**. (Or say **"approve"**.)
3. Watch each item turn **Verified**, with a link to the task or event. That means Jarvis **read it back from Todoist/Google** — not just that it sent the request.

Good to know:
- Changing anything creates a **new version**; an older approval never carries over.
- Jarvis never takes "sounds good" as approval, and ignores "approve" if it hears it while it's speaking.
- If an item shows **Unknown**, the provider may have received it — press **Retry**; Jarvis checks for it first and won't create a duplicate.

### The Today page

Your date, the day's **mission and top 3 priorities** (click **Set plan** to set or edit), agenda, due and overdue tasks, deadlines, conflicts, and open work windows. Each source shows a badge — **OK**, **Stale**, or **Not connected** — so a blank list never pretends everything is clear.

### Memory

**Memory** shows everything Jarvis knows about you, where it came from, and when. You can **edit** (corrections replace old versions), **pin**, **forget**, or **export**.

---

## Part 9 — Safety controls you should know

| Control | Where | What it does |
|---|---|---|
| **Pause actions** | Top-right button, every page | Stops all new writes immediately. Resuming does **not** re-run old approvals — you re-approve. |
| **Stop speaking** | Command page | Silences Jarvis's voice only |
| **Cancel this run** | Under a response | Stops the current request |
| **Reject** | Proposal card | Discards a plan |
| **Activity** | Sidebar | Plain-language log of everything Jarvis read, proposed, and did |
| **Actions → History** | Sidebar | What you asked, what was approved, what changed, proof, and how to undo |

Jarvis **cannot**: send emails/texts/Slack, send calendar invites, buy anything, touch grades or money, or run shell commands. It can draft messages for you to send yourself.

---

## Part 10 — Backups

- **Make a backup:** **Settings → Backups → Create backup**, or `uv run jarvis backup`.
- **Restore:** in the same screen choose a backup → review the preview → confirm. Jarvis first saves your current state, then restores with **actions paused** and **schedules off**, so nothing old re-runs. Review, then resume.
- **Keep separately:** `backend/data/secret.key` and your `.env` are **not** inside backups on purpose. Store them somewhere safe.

---

## Part 11 — Troubleshooting

| Problem | Fix |
|---|---|
| `localhost:8765` won't load | Make sure Terminal 1 is running `uv run jarvis serve`. In WSL, try `http://127.0.0.1:8765`. |
| "Host not allowed" | Use `localhost` or `127.0.0.1`, or add your hostname to `JARVIS_ALLOWED_HOSTS` in `.env` (phone setup). |
| Mic button shows a permission error | Browser address bar → site settings → Microphone → Allow, then reload. Typing always works. |
| "Speech-to-text unavailable" | Run `uv run jarvis stt-warmup`; it needs internet the first time to download the model. |
| Transcripts are often wrong | Speak a bit slower and closer to the mic. For better accuracy, set `JARVIS_STT_MODEL=small.en` in `.env` (bigger download, slower). |
| "Approve" by voice isn't accepted | Jarvis wasn't confident it heard you — tap **Approve**, or lower `JARVIS_STT_MIN_CONFIDENCE` (default 0.55) in `.env`. |
| Approved items stay "Queued" | The worker isn't running — start Terminal 2 (`uv run jarvis worker`). Also check you haven't hit **Pause actions**. |
| Google says `redirect_uri_mismatch` | The redirect URI in Google Cloud must be exactly `http://localhost:8765/api/integrations/google/callback`. |
| Google says "Access blocked" | Add your Gmail as a **Test user** on the OAuth consent screen. |
| Briefing didn't arrive | Computer asleep, or worker not running. **Settings → Schedules** shows the last success; generate one on demand from **Briefings**. |
| Forgot the password | In Ubuntu: `cd ~/dev/JARVIS/backend && uv run jarvis reset-password`. This signs out all devices, so re-pair your phone afterward. |
| Anything else | `uv run jarvis doctor` explains what's wrong and how to fix it. |

---

## Quick reference card

```bash
# Start (two terminals)
cd ~/dev/JARVIS/backend && uv run jarvis serve
cd ~/dev/JARVIS/backend && uv run jarvis worker

# Health check
uv run jarvis doctor

# Update to the latest code
cd ~/dev/JARVIS && git pull
cd backend && uv sync
cd ../frontend && npm ci && npm run build
# then restart serve + worker

# Backup / restore
uv run jarvis backup
uv run jarvis restore <backup-id> --yes
```

**Open:** <http://localhost:8765>  ·  **More detail:** `README.md`, `DEPLOYMENT.md`, `ARCHITECTURE.md`, `LIVE_VERIFICATION.md`
