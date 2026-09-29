"""First-run seed: editable, dated user context (NOT hardcoded instructions), projects, default
schedules (shown disabled until AJ activates them), and unconfirmed course-mapping suggestions."""
from __future__ import annotations

import sqlite3

from jarvis.core.db import jdump, row
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
    "budget": {"paid_inference_enabled": False, "background_paid_enabled": False, "daily_limit_usd": None,
               "monthly_limit_usd": None, "max_output_tokens": 4000, "max_tool_calls": 6, "max_specialists": 3,
               "effort": "low", "chosen": False},
    "models": {"chief": "claude-opus-5-5", "vision": "claude-opus-5-5", "briefing": "claude-opus-5-5"},
    "todoist": {"area_projects": {}},
}


def seed(conn: sqlite3.Connection) -> bool:
    if row(conn, "SELECT id FROM user_settings WHERE id=1"):
        return False
    now = now_iso()
    conn.execute("INSERT INTO user_settings (id, display_name, timezone, data, updated_at) VALUES (1,'AJ','America/Chicago',?,?)",
                 (jdump(DEFAULT_SETTINGS), now))
    conn.execute("INSERT OR IGNORE INTO control_state (id, actions_paused) VALUES (1, 0)")
    slug_to_id = {}
    for slug, name, area, desc, sensitive in PROJECTS:
        pid = new_id("proj")
        slug_to_id[slug] = pid
        conn.execute("INSERT INTO projects (id, slug, name, area, description, sensitive, created_at, updated_at) VALUES "
                     "(?,?,?,?,?,?,?,?)", (pid, slug, name, area, desc, sensitive, now, now))
    for content, type_, slug, entity, pinned, review, expires, eff in CONTEXT:
        memory.add(conn, content, type_, "seed", source_ref=SEED_REF, project_id=slug_to_id.get(slug) if slug else None,
                   entity=entity, pinned=pinned, review_at=review, expires_at=expires, effective_from=eff,
                   supersede_similar=False)
    for key, wf, label, h, m, wds, grace in SCHEDULES:
        conn.execute("INSERT INTO schedules (id, key, workflow, label, tz, hour, minute, weekdays, enabled, misfire_grace_minutes, "
                     "destination, created_at, updated_at) VALUES (?,?,?,?, 'America/Chicago', ?,?,?, 0, ?, 'inbox', ?,?)",
                     (new_id("sch"), key, wf, label, h, m, jdump(wds), grace, now, now))
    for pat, course, role in COURSE_SUGGESTIONS:
        conn.execute("INSERT INTO course_mappings (id, pattern, course, role, confirmed, created_at) VALUES (?,?,?,?,0,?)",
                     (new_id("cm"), pat, course, role, now))
    return True
