"""Command splitting regressions (reported: greeting became a task; grocery + grading merged)."""
from datetime import UTC, datetime

import pytest

from jarvis.agents.interpret import classify, extract_commitments

REF = datetime(2026, 9, 29, 20, tzinfo=UTC)   # Tue 3 PM CDT


@pytest.mark.parametrize("text,expected", [
    ("Yo Jarvis, go grocery shopping today and then remind me to grade MSC 550 and IS 640 tomorrow.",
     [("Go grocery shopping", "2026-09-29"), ("Grade MSC 550 and IS 640", "2026-09-30")]),
    ("Yo Jarvis go grocery shopping today and then Remind me to grade MSC 550 and IS 640 tomorrow",
     [("Go grocery shopping", "2026-09-29"), ("Grade MSC 550 and IS 640", "2026-09-30")]),
    ("Go grocery shopping today and then I want you to remind me to grade MSC 550 and IS 640 tomorrow",
     [("Go grocery shopping", "2026-09-29"), ("Grade MSC 550 and IS 640", "2026-09-30")]),
    ("Jarvis I need to go grocery shopping today I also need to grade IS 640 tomorrow",
     [("Go grocery shopping", "2026-09-29"), ("Grade IS 640", "2026-09-30")]),
    ("Buy bread and butter tomorrow", [("Buy bread and butter", "2026-09-30")]),
    ("Grade MSC 550 and IS 640 homework Friday", [("Grade MSC 550 and IS 640 homework", "2026-10-02")]),
    ("call mom, email Yi Tan about the rubric, and pick up dry cleaning Thursday",
     [("Call mom", None), ("Email Yi Tan about the rubric", None), ("Pick up dry cleaning", "2026-10-01")]),
    ("Okay Jarvis, can you remind me to pay rent on Friday, thanks", [("Pay rent", "2026-10-02")]),
])
def test_split(text, expected):
    got = [(c.content, c.when.date) for c in extract_commitments(text, REF, "America/Chicago")]
    assert got == expected


def test_greeting_alone_is_not_a_task():
    i = classify("Hey Jarvis", has_active_proposal=False, has_attachments=False, captured_at=REF, tz="America/Chicago")
    assert not i.commitments
