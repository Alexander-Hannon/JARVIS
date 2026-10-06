"""Seeds. `seed()` prepares the org (control state, org settings) on every start; `seed_user()` prepares one person
when they're created: settings, disabled default schedules, and — for the first owner of a *personal* install only —
AJ's editable, dated context (projects, memories, unconfirmed course mappings). Other users start clean."""
from __future__ import annotations

import sqlite3

from jarvis.core.db import jdump, jload, row
from jarvis.core.util import new_id, now_iso
from jarvis.memory import service as memory

SEED_DATE = "2026-09-28"
SEED_REF = f"JARVIS_BUILD_SPEC.md §2 (seeded {SEED_DATE})"

PROJECTS = [
    ("school", "School — own courses", "school", "Engineering Management Theory; Managerial Economics & Tech; Marketing Strategy & Analysis (MKT 601).", 0),
    ("gta", "GTA / TA duties", "gta", "IS 640 and MSC 550 with Yi Tan; IS 401/501 with DJ Hovermale. Grading and student material stays in this project.", 1),
    ("business", "Business & income", "business", "Rumble work; iFlex remote sales; reliable income.", 0),
    ("coaching", "Coaching / personal training", "coaching", "Personal-training clients. Client data only from current user-supplied sources.", 1),
    ("faith", "Faith", "faith", "Catholic faith; Tuesday-evening CCM leadership.", 0),
    ("training", "Health & training", "training", "Monk Mode challenge; Operation Cruise.", 0),
    ("ai-guru", "AI GURU", "ai_guru", "AI GURU project and long-term Stratosphere goals.", 0),
    ("admin", "Admin & personal", "admin", "Errands, bills supplied by AJ, household.", 0),
]

# (content, type, project_slug, entity, pinned, review_at, expires_at, effective_from)
CONTEXT = [
    ("AJ Hannon; home timezone America/Chicago; location Huntsville, Alabama.", "fact", None, "profile.basics", True, None, None, None),
    ("Priorities: Catholic faith, health/training, school and GTA responsibilities, reliable income, coaching/business, AI GURU, long-term Stratosphere goals.", "goal", None, "profile.priorities", True, "2026-12-15", None, None),
    ("Communication: concise, direct, practical, conversational; lead with the priority and next action; avoid long undifferentiated task lists and repetitive permission prompts.", "preference", None, "pref.communication", True, None, None, None),
    ("AJ does schoolwork during GTA shifts (generic recurring study blocks were removed). Use a prioritized, conditional work queue inside GTA windows — conditional on assigned GTA duties and actual free time. Do not recreate generic study blocks.", "preference", "gta", "pref.schoolwork_during_gta", True, None, None, None),
    ("Keep student (own coursework) responsibilities separate from grading responsibilities.", "constraint", "gta", "pref.separate_student_grading", False, None, None, None),
    ("Tuesday-evening CCM leadership is a planning constraint; exact times must come from the calendar or AJ.", "constraint", "faith", "constraint.ccm_tuesday", False, None, None, None),
    ("Monk Mode challenge started 2026-09-23: 2 × 45-minute workouts, a gallon of water, zero alcohol, reading, progress photo; outdoor requirement is user-defined/flexible.", "context", "training", "challenge.monk_mode", False, "2026-10-23", None, "2026-09-23"),
    ("Operation Cruise runs through 2026-12-11.", "context", "training", "challenge.operation_cruise", False, None, "2026-12-11", None),
    ("Nutrition targets are evolving: load current approved targets from AJ; do not import old contest-prep macros.", "constraint", "training", "constraint.nutrition", False, "2026-10-15", None, None),
    ("A separate challenge 'Commit' reminder is at 5:30 AM — distinct from the 6:30 AM Daily Command briefing.", "fact", "training", "reminder.commit_0530", False, None, None, None),
    ("Financial and client data (income, balances, check-ins, client commitments) must come from current user-supplied sources; never invent them.", "constraint", None, "constraint.no_invented_finance", False, None, None, None),
]

SCHEDULES = [
    ("daily_command", "daily_command", "Daily Command", 6, 30, [0, 1, 2, 3, 4, 5, 6], 120),
    ("nightly_debrief", "nightly_debrief", "Nightly Debrief", 20, 30, [0, 1, 2, 3, 4, 5, 6], 150),
    ("weekly_war_room", "weekly_war_room", "Weekly War Room", 19, 0, [6], 240),
]

COURSE_SUGGESTIONS = [
    ("IS 640", "IS 640", "gta"), ("MSC 550", "MSC 550", "gta"), ("IS 401", "IS 401", "gta"), ("IS 501", "IS 501", "gta"),
    ("MKT 601", "MKT 601 Marketing Strategy & Analysis", "own"),
    ("Engineering Management", "Engineering Management Theory", "own"),
    ("Managerial Economics", "Managerial Economics & Tech", "own"),
]

DEFAULT_SETTINGS = {
    "voice": {"voice_name": None, "rate": 1.05, "muted": False, "auto_speak": True, "tts_engine": "jarvis",
              "tts_preset": "jarvis", "auto_send": True},
    "privacy": {"retain_audio": False},
    "notifications": {"lock_screen_details": False, "push_kinds": {"briefing": True, "action_result": False}},
    "todoist": {"area_projects": {}},
    "weather": {"enabled": True, "provider": "open-meteo"},
}
# AJ's personal install: how he's addressed and where he is (other users set their own in Settings → Account).
PERSONAL_SETTINGS = {"location": {"name": "Huntsville, AL", "lat": 34.7304, "lon": -86.5861},
                     "address_as": {"name": "AJ", "honorific": "sir", "pronoun": "he"}}
PERSONAL_TZ = "America/Chicago"

DEFAULT_ORG = {
    "budget": {"paid_inference_enabled": False, "background_paid_enabled": False, "daily_limit_usd": None,
               "monthly_limit_usd": None, "max_output_tokens": 4000, "max_tool_calls": 6, "max_specialists": 3,
               "effort": "low", "chosen": False},
    "models": {"chief": "claude-opus-5-5", "vision": "claude-opus-5-5", "briefing": "claude-opus-5-5"},
}


def seed(conn: sqlite3.Connection) -> bool:
    """Org-level defaults; safe to run on every start."""
    conn.execute("INSERT OR IGNORE INTO control_state (id, actions_paused) VALUES (1, 0)")
    org = jload((row(conn, "SELECT data FROM org_settings WHERE id=1") or {}).get("data"), {}) or {}
    missing = {k: v for k, v in DEFAULT_ORG.items() if not org.get(k)}
    if not missing:
        return False
    conn.execute("INSERT INTO org_settings (id, data, updated_at) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET data=?, "
                 "updated_at=?", (jdump(org | missing), now_iso(), jdump(org | missing), now_iso()))
    return True


def seed_user(conn: sqlite3.Connection, uid: str, display_name: str, tz: str | None = None) -> bool:
    if row(conn, "SELECT user_id FROM user_settings WHERE user_id=?", (uid,)):
        return False
    from jarvis.config import get_settings
    personal = get_settings().edition == "personal" and uid == "usr_owner"
    now = now_iso()
    tz = tz or (PERSONAL_TZ if personal else "UTC")
    data = DEFAULT_SETTINGS | (PERSONAL_SETTINGS if personal else {"address_as": {"name": display_name, "honorific": None}})
    conn.execute("INSERT INTO user_settings (user_id, timezone, data, updated_at) VALUES (?,?,?,?)",
                 (uid, tz, jdump(data), now))
    for key, wf, label, h, m, wds, grace in SCHEDULES:
        conn.execute("INSERT INTO schedules (id, user_id, key, workflow, label, tz, hour, minute, weekdays, enabled, "
                     "misfire_grace_minutes, destination, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,0,?,'inbox',?,?)",
                     (new_id("sch"), uid, key, wf, label, tz, h, m, jdump(wds), grace, now, now))
    if not personal:
        return True
    slug_to_id = {}
    for slug, name, area, desc, sensitive in PROJECTS:
        pid = new_id("proj")
        slug_to_id[slug] = pid
        conn.execute("INSERT INTO projects (id, user_id, slug, name, area, description, sensitive, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (pid, uid, slug, name, area, desc, sensitive, now, now))
    for content, type_, slug, entity, pinned, review, expires, eff in CONTEXT:
        memory.add(conn, uid, content, type_, "seed", source_ref=SEED_REF,
                   project_id=slug_to_id.get(slug) if slug else None, entity=entity, pinned=pinned, review_at=review,
                   expires_at=expires, effective_from=eff, supersede_similar=False)
    for pat, course, role in COURSE_SUGGESTIONS:
        conn.execute("INSERT INTO course_mappings (id, user_id, pattern, course, role, confirmed, created_at) "
                     "VALUES (?,?,?,?,?,0,?)", (new_id("cm"), uid, pat, course, role, now))
    return True
