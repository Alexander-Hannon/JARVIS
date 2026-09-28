"""Deterministic relative date/time resolution anchored to the capture instant and user timezone.

Rules:
- The reference is the moment the user spoke (captured_at) converted to their IANA timezone, never
  the processing time. "Tomorrow" said at 23:58 stays tomorrow-relative-to-23:58.
- Vague phrases ("soon", "later", "sometime", "asap") never become deadlines; they are reported
  as `vague` so the UI can show the assumption / ask.
- Every resolution carries the verbatim phrase and an assumption string for display.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
MON_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"sept": 9}
VAGUE = ["soon", "later", "sometime", "some time", "asap", "eventually", "at some point", "when i can"]

_WD_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)"
_POD = r"(?:morning|afternoon|evening|night)"
_MON_RE = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"


@dataclass
class When:
    phrase: str | None = None           # verbatim date phrase found
    date: str | None = None             # ISO local date
    time: str | None = None             # HH:MM local, only if user said a time
    time_phrase: str | None = None
    part_of_day: str | None = None      # morning|afternoon|evening|night (preferred time, not a due time)
    role: str = "scheduled"             # scheduled (do-date) | due (deadline: "by", "due")
    duration_min: int | None = None
    vague: str | None = None            # vague phrase detected; no date invented
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def local_ref(captured_at: datetime, tz: str) -> datetime:
    return captured_at.astimezone(ZoneInfo(tz))


def _next_weekday(ref: date, wd: int, force_next_week: bool = False) -> date:
    delta = (wd - ref.weekday()) % 7
    if delta == 0:
        delta = 7  # "Thursday" said on a Thursday means next Thursday; stated as an assumption
    if force_next_week and delta < 7:
        # "next Friday" = the Friday of next week (common US usage) — stated as assumption
        start_next_week = ref + timedelta(days=(7 - ref.weekday()))
        return start_next_week + timedelta(days=wd)
    return ref + timedelta(days=delta)


def _parse_time(text: str) -> tuple[str | None, str | None]:
    m = re.search(r"\b(?:at|by|@)\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", text, re.IGNORECASE)
    if not m:
        m2 = re.search(r"\b(noon|midnight)\b", text, re.IGNORECASE)
        if m2:
            return ("12:00" if m2.group(1).lower() == "noon" else "23:59"), m2.group(0)
        return None, None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    if h > 23 or mi > 59:
        return None, None
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif not ampm and 1 <= h <= 6:
        h += 12  # "at 3" -> 15:00 is the practical reading; surfaced as an assumption by caller
    return f"{h:02d}:{mi:02d}", m.group(0)


def _parse_duration(text: str) -> tuple[int | None, str | None]:
    m = re.search(r"\b(\d{1,3})\s*(?:-|\s)?(minutes?|mins?|hours?|hrs?|h)\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(an?|one|half an?)\s+(hour)\b", text, re.IGNORECASE)
        if m:
            return (30 if m.group(1).lower().startswith("half") else 60), m.group(0)
        return None, None
    n = int(m.group(1))
    unit = m.group(2).lower()
    return (n * 60 if unit.startswith("h") else n), m.group(0)


def resolve(text: str, captured_at: datetime, tz: str) -> When:
    """Find the first date expression in `text` relative to captured_at (aware) in tz."""
    ref = local_ref(captured_at, tz)
    today = ref.date()
    low = text.lower()
    w = When()

    deadline = re.search(r"\b(by|due|before|no later than)\b", low)

    for v in VAGUE:
        if re.search(rf"\b{re.escape(v)}\b", low):
            w.vague = v
            w.assumptions.append(f"'{v}' is not a deadline — no date set.")
            break

    patterns: list[tuple[str, callable]] = [
        (r"\bday after tomorrow\b", lambda m: today + timedelta(days=2)),
        (r"\btomorrow (?:morning|afternoon|evening|night)\b|\btomorrow\b|\btmrw\b", lambda m: today + timedelta(days=1)),
        (r"\btonight\b|\bthis (?:morning|afternoon|evening)\b|\btoday\b|\blater today\b", lambda m: today),
        (r"\bin (\d{1,2}) days?\b", lambda m: today + timedelta(days=int(m.group(1)))),
        (r"\bin a week\b|\bnext week\b", None),  # handled below (ambiguous -> assumption)
        (rf"\bnext {_WD_RE}(?: {_POD})?\b", lambda m: _next_weekday(today, _wd(m.group(1)), force_next_week=True)),
        (rf"\b(?:this |on |by |due )?{_WD_RE}(?: {_POD})?\b", lambda m: _next_weekday(today, _wd(m.group(1)))),
        (rf"\b{_MON_RE}\.? (\d{{1,2}})(?:st|nd|rd|th)?\b", lambda m: _month_day(today, _mon(m.group(1)), int(m.group(2)))),
        (r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lambda m: _slash_date(today, m)),
    ]
    for pat, fn in patterns:
        m = re.search(pat, low)
        if not m:
            continue
        phrase = text[m.start():m.end()]
        if fn is None:
            w.phrase = phrase
            w.assumptions.append(f"'{phrase}' has no specific day — left unscheduled; tell me a day if needed.")
            w.vague = w.vague or phrase
            break
        try:
            d = fn(m)
        except ValueError:
            continue
        if d is None:
            continue
        w.phrase = phrase.strip()
        w.date = d.isoformat()
        wdm = re.search(_WD_RE, m.group(0))
        if wdm and "next" not in m.group(0) and _wd(wdm.group(1)) == today.weekday():
            w.assumptions.append(f"Today is {WEEKDAYS[today.weekday()].title()}; '{phrase.strip()}' read as next week's.")
        if "next" in m.group(0) and wdm:
            w.assumptions.append(f"'{phrase.strip()}' read as {d.strftime('%a %b %d')} (the {WEEKDAYS[d.weekday()].title()} of next week).")
        pl = m.group(0)
        if "tonight" in pl or "night" in pl or "evening" in pl:
            w.part_of_day = "evening"
        elif "morning" in pl:
            w.part_of_day = "morning"
        elif "afternoon" in pl:
            w.part_of_day = "afternoon"
        break

    t, tphrase = _parse_time(text)
    if t:
        w.time, w.time_phrase = t, tphrase
        if not re.search(r"(a\.?m\.?|p\.?m\.?|noon|midnight|:\d{2})", tphrase or "", re.IGNORECASE):
            w.assumptions.append(f"'{tphrase.strip()}' read as {t}.")
        if not w.date:
            # A bare time refers to today unless already past at capture time, then tomorrow.
            hh, mm = map(int, t.split(":"))
            d = today if time(hh, mm) > ref.time() else today + timedelta(days=1)
            w.date = d.isoformat()
            w.assumptions.append(f"No day given; '{tphrase.strip()}' read as {d.strftime('%a %b %d')}.")
    dur, _ = _parse_duration(text)
    w.duration_min = dur
    if deadline and w.date:
        w.role = "due"
    if w.part_of_day and not w.time:
        w.assumptions.append(f"'{w.phrase}' kept as a preferred time of day ({w.part_of_day}), not a due time.")
    if w.date and w.role == "scheduled" and not w.time and not w.part_of_day:
        w.assumptions.append(f"'{w.phrase}' used as the Todoist date to do it — not a hard deadline.")
    return w


def _wd(s: str) -> int:
    s = s.lower()
    if s in WEEKDAYS:
        return WEEKDAYS.index(s)
    return WD_ABBR[s]


def _mon(s: str) -> int:
    s = s.lower().rstrip(".")
    if s in MONTHS:
        return MONTHS.index(s) + 1
    return MON_ABBR[s[:4] if s.startswith("sept") else s[:3]]


def _month_day(today: date, month: int, day: int) -> date:
    d = date(today.year, month, day)
    if d < today - timedelta(days=30):
        d = date(today.year + 1, month, day)
    return d


def _slash_date(today: date, m: re.Match) -> date | None:
    mo, dy = int(m.group(1)), int(m.group(2))
    yr = m.group(3)
    if yr:
        y = int(yr)
        y = y + 2000 if y < 100 else y
        return date(y, mo, dy)
    return _month_day(today, mo, dy)


def strip_when(text: str, w: When) -> str:
    """Remove the resolved date/time phrases from task content."""
    out = text
    for p in (w.phrase, w.time_phrase):
        if p:
            out = re.sub(rf"\s*\b(?:on |by |due |this |at )?{re.escape(p.strip())}\b", "", out, count=1, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip(" ,.;")


def to_utc_instant(local_date: str, local_time: str, tz: str) -> datetime:
    """Local wall time -> aware datetime. Nonexistent (spring-forward) times shift forward by the gap;
    ambiguous (fall-back) times use the first occurrence (fold=0)."""
    zi = ZoneInfo(tz)
    naive = datetime.combine(date.fromisoformat(local_date), time.fromisoformat(local_time))
    aware = naive.replace(tzinfo=zi, fold=0)
    # Detect nonexistent time: round-trip through UTC changes wall clock.
    rt = aware.astimezone(ZoneInfo("UTC")).astimezone(zi)
    if rt.replace(tzinfo=None) != naive:
        aware = rt  # PEP 495 semantics already moved it forward by the gap
    return aware
