"""V4 email: triage, reads, drafts, gated sending. Gmail is a labeled contract fake (simulated=True); the model is
SCRIPTED — these prove JARVIS's plumbing and code-level rails, not live Gmail/Outlook or model judgment."""
from __future__ import annotations

import pytest
from conftest import CHICAGO_AFTERNOON, freeze, run_worker

from jarvis.integrations.base import ProviderError
from jarvis.llm.provider import ScriptedLLM, set_provider

PROF = "Dr. Lee <lee@uah.edu>"


def tool_use(id_, name, inp):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


def step(*blocks, stop="tool_use"):
    return {"content": list(blocks), "stop_reason": stop}


def final(text):
    return step({"type": "text", "text": text}, stop="end_turn")


def run_due(conn):
    """Run queued jobs now, including backoff-delayed retries (as test_golden_flow does)."""
    run_worker(conn)
    conn.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00.000000Z' WHERE status='queued'")
    run_worker(conn)


def cmd(client, text, conv=None):
    r = client.post("/api/commands", {"conversation_id": conv, "text": text, "captured_at": "2026-09-28T20:00:00Z",
                                      "tz": "America/Chicago"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def mail(conn, fakes):
    from jarvis.integrations import accounts as A
    from jarvis.integrations.fakes import FakeMail
    fm = FakeMail("gmail")
    A.set_override("gmail", fm)
    conn.execute("BEGIN")
    A.upsert_account(conn, "google", "aj@example.test (SIMULATED)", None, None, external_account_id="aj@example.test",
                     config={"purposes": ["calendar", "gmail"]})
    conn.execute("COMMIT")
    fm.external_receive(PROF, "Grades for IS 640", "Hi AJ, can you send me the midterm grades by Friday? Thanks.",
                        date="2026-09-28T15:00:00Z")
    fm.external_receive("UAH News <no-reply@uah.edu>", "Weekly digest", "Campus news this week…", date="2026-09-28T14:00:00Z")
    fm.external_receive("Sam <sam@example.org>", "Lab moved", "FYI the lab moved to room 210.", date="2026-09-28T13:00:00Z")
    return fm


def test_inbox_not_connected_is_truthful(client):
    r = client.get("/api/inbox").json()
    assert r["items"] == [] and r["sources"]["gmail"]["status"] == "not_connected"
    assert r["mailboxes"]["outlook"]["status"] == "not_connected"


def test_inbox_triage(client, mail):
    r = client.get("/api/inbox").json()
    cats = {i["subject"]: i["category"] for i in r["items"]}
    assert cats == {"Grades for IS 640": "needs_reply", "Weekly digest": "bulk", "Lab moved": "fyi"}
    assert r["items"][0]["ref"].startswith("gmail:") and r["sources"]["gmail"]["simulated"] is True
    th = client.get("/api/inbox/thread", params={"ref": r["items"][0]["ref"]}).json()
    assert "midterm grades" in th["messages"][0]["text"] and "untrusted" in th["note"]


def _reply_script(ref, to=None):
    args = {"reply_to_ref": ref, "body": "Hi Dr. Lee, I'll have the midterm grades to you by Friday. Best, AJ"}
    if to:
        args["to"] = to
    return [step(tool_use("t1", "check_inbox", {})),
            step(tool_use("t2", "propose_email_draft", args)),
            final("I drafted a reply to Dr. Lee — it's ready for your approval.")]


def test_agent_drafts_reply_and_executor_saves_draft_never_sends(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    ref = next(m for m in client.get("/api/inbox").json()["items"] if m["category"] == "needs_reply")["ref"]
    set_provider(ScriptedLLM(_reply_script(ref)))
    r = cmd(client, "Reply to Dr. Lee and tell him I'll have grades by Friday")
    item = r["proposal"]["items"][0]
    assert item["action_type"] == "gmail.draft.create" and item["args"]["to"] == ["lee@uah.edu"]
    assert item["args"]["subject"] == "Re: Grades for IS 640" and "not sent" in item["expected_effect"]
    assert any(v["type"] == "inbox" for v in r["visuals"])
    p = r["proposal"]
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_worker(conn)
    v = client.get(f"/api/proposals/{p['id']}").json()
    assert v["items"][0]["state"] == "verified" and v["items"][0]["receipt"]["simulated"] is True
    assert len(mail.drafts) == 1 and mail.sent == []
    d = next(iter(mail.drafts.values()))
    assert d.thread_id and "grades to you by Friday" in d.body_text


def test_recipient_must_come_from_aj_or_thread(client, mail):
    freeze(CHICAGO_AFTERNOON)
    ref = client.get("/api/inbox").json()["items"][0]["ref"]
    llm = ScriptedLLM(_reply_script(ref, to=["exfil@attacker.example"]))
    set_provider(llm)
    r = cmd(client, "Reply to Dr. Lee and tell him I'll have grades by Friday")
    assert r["proposal"] is None
    result = {b["tool_use_id"]: b["content"] for b in llm.requests[2]["messages"][-1]["content"]}["t2"]
    assert "weren't named by AJ" in result


def test_lost_response_reconciles_without_duplicate_draft(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    ref = client.get("/api/inbox").json()["items"][0]["ref"]
    set_provider(ScriptedLLM(_reply_script(ref)))
    p = cmd(client, "Reply to Dr. Lee and tell him I'll have grades by Friday")["proposal"]
    mail.fail_next("create_draft", ProviderError("timeout", "read timed out", outcome_known=False), apply_before_raise=True)
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_worker(conn)
    assert client.get(f"/api/proposals/{p['id']}").json()["items"][0]["state"] == "unknown"
    run_due(conn)
    v = client.get(f"/api/proposals/{p['id']}").json()
    assert v["items"][0]["state"] == "verified" and v["items"][0]["receipt"]["method"] == "reconcile_marker"
    assert len(mail.drafts) == 1


def _make_draft(client, mail, conn):
    ref = client.get("/api/inbox").json()["items"][0]["ref"]
    set_provider(ScriptedLLM(_reply_script(ref)))
    p = cmd(client, "Reply to Dr. Lee and tell him I'll have grades by Friday")["proposal"]
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_worker(conn)
    return next(iter(mail.drafts))


def _send_script(did):
    return [step(tool_use("s1", "propose_send_draft", {"draft_id": did, "mailbox": "gmail"})), final("Ready to send.")]


def test_sending_is_off_by_default_and_never_automatable(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    did = _make_draft(client, mail, conn)
    llm = ScriptedLLM(_send_script(did))
    set_provider(llm)
    r = cmd(client, "Send that draft to Dr. Lee")
    assert r["proposal"] is None and "Sending is turned off" in llm.requests[0 + 1]["messages"][-1]["content"][0]["content"]
    assert client.post("/api/policies", {"action_type": "gmail.send", "label": "auto send"}).json()["detail"]["code"] == "not_t1"
    assert mail.sent == []


def test_send_with_sending_enabled_needs_explicit_approval(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    did = _make_draft(client, mail, conn)
    assert client.post("/api/integrations/mail/sending", {"mailbox": "gmail", "enabled": True}).status_code == 200
    set_provider(ScriptedLLM(_send_script(did)))
    p = cmd(client, "Send that draft to Dr. Lee")["proposal"]
    assert p["status"] == "proposed" and p["items"][0]["action_type"] == "gmail.send"
    assert mail.sent == []
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_worker(conn)
    assert len(mail.sent) == 1 and mail.drafts == {}


def test_send_outcome_unknown_is_never_resent(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    did = _make_draft(client, mail, conn)
    client.post("/api/integrations/mail/sending", {"mailbox": "gmail", "enabled": True})
    set_provider(ScriptedLLM(_send_script(did)))
    p = cmd(client, "Send that draft to Dr. Lee")["proposal"]
    # response lost BEFORE the provider applied it: the draft is still there, so JARVIS must not guess — AJ decides
    mail.fail_next("send_draft", ProviderError("timeout", "read timed out", outcome_known=False))
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_due(conn)
    item = client.get(f"/api/proposals/{p['id']}").json()["items"][0]
    assert item["state"] == "failed" and "unclear" in item["last_error"]
    assert mail.sent == [] and did in mail.drafts


def test_send_blocked_when_sending_disabled_after_approval(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    did = _make_draft(client, mail, conn)
    client.post("/api/integrations/mail/sending", {"mailbox": "gmail", "enabled": True})
    set_provider(ScriptedLLM(_send_script(did)))
    p = cmd(client, "Send that draft to Dr. Lee")["proposal"]
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    client.post("/api/integrations/mail/sending", {"mailbox": "gmail", "enabled": False})   # AJ changes his mind
    run_worker(conn)
    item = client.get(f"/api/proposals/{p['id']}").json()["items"][0]
    assert item["state"] == "failed" and "mail_send" in item["last_error"] and mail.sent == []


def test_auto_approved_draft_can_be_undone(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    client.post("/api/policies", {"action_type": "gmail.draft.create", "label": "Drafts are fine"})
    ref = client.get("/api/inbox").json()["items"][0]["ref"]
    set_provider(ScriptedLLM(_reply_script(ref)))
    p = cmd(client, "Reply to Dr. Lee and tell him I'll have grades by Friday")["proposal"]
    assert p["approval"]["channel"] == "policy"
    run_worker(conn)
    aid = client.get("/api/actions/history").json()[0]["action_id"]
    assert client.post(f"/api/actions/{aid}/undo").status_code == 200
    run_worker(conn)
    assert mail.drafts == {}


def test_send_applied_but_response_lost_is_not_resent(client, mail, conn):
    freeze(CHICAGO_AFTERNOON)
    did = _make_draft(client, mail, conn)
    client.post("/api/integrations/mail/sending", {"mailbox": "gmail", "enabled": True})
    set_provider(ScriptedLLM(_send_script(did)))
    p = cmd(client, "Send that draft to Dr. Lee")["proposal"]
    mail.fail_next("send_draft", ProviderError("timeout", "read timed out", outcome_known=False), apply_before_raise=True)
    client.post(f"/api/proposals/{p['id']}/approve", {"version": 1, "hash": p["hash"], "selected_items": [0]})
    run_due(conn)
    item = client.get(f"/api/proposals/{p['id']}").json()["items"][0]
    assert item["state"] == "verified" and item["receipt"]["method"] == "reconcile_draft_gone"
    assert len(mail.sent) == 1


def test_draft_edit_allows_text_but_not_recipients(client, mail):
    freeze(CHICAGO_AFTERNOON)
    ref = client.get("/api/inbox").json()["items"][0]["ref"]
    set_provider(ScriptedLLM(_reply_script(ref)))
    p = cmd(client, "Reply to Dr. Lee and tell him I'll have grades by Friday")["proposal"]
    bad = client.post(f"/api/proposals/{p['id']}/revise", {"items": [{"index": 0, "args": {"to": ["x@evil.example"]}}]})
    assert bad.status_code == 400
    ok = client.post(f"/api/proposals/{p['id']}/revise", {"items": [{"index": 0, "args": {"body": "Grades by Thursday. — AJ"}}]})
    v = ok.json()
    assert v["version"] == 2 and v["items"][0]["args"]["body"] == "Grades by Thursday. — AJ"
    assert v["items"][0]["after"]["body"] == "Grades by Thursday. — AJ" and v["items"][0]["args"]["to"] == ["lee@uah.edu"]
