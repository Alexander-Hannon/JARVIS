"""Deterministic command understanding (always available, no LLM needed).

Produces an Interpretation: intent + commitments + references. The optional model path
(agents/llm_extract.py) must produce the same structure and is validated against the transcript
(date phrases must literally occur in what AJ said) so the model cannot invent deadlines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from jarvis.core import timeparse

LEAD_INS = [
    r"i also need to", r"i need to", r"i have to", r"i've got to", r"i got to", r"i gotta", r"i should", r"i must",
    r"remind me to", r"don't let me forget to", r"please", r"can you", r"could you", r"add a task to", r"add a to-?do to",
    r"add task", r"add", r"to-?do:?", r"also", r"and", r"then", r"jarvis,?", r"hey jarvis,?", r"ok(ay)?,?",
    r"i want you to", r"i need you to", r"i'd like you to", r"will you", r"would you", r"don't forget to",
    r"make sure to", r"make sure i", r"set a reminder to", r"after that", r"plus",
]
_LEAD_RE = re.compile(r"^\s*(?:" + "|".join(LEAD_INS) + r")\b[\s,:]*", re.IGNORECASE)
VERBS = set(["shop", "remind", "see", "talk", "tell", "look", "find", "install", "move", "sell", "try", "help", "watch",
              "listen", "pack", "charge", "feed", "walk", "water", "wake", "donate", "deliver", "schedule", "meet", "have",
              "grade", "follow", "buy", "call", "email", "text", "send", "schedule", "book", "finish", "submit", "write", "read", "review", "pay", "pick", "prepare", "prep", "meet", "check", "update", "clean", "go", "do", "study", "work", "plan", "create", "draft", "fix", "reply", "respond", "order", "return", "cancel", "renew", "practice", "train", "run", "lift", "pray", "attend", "set", "make", "get", "file", "print", "post", "upload", "record", "research", "contact", "ask", "start", "complete", "wash", "cook", "drop", "bring", "take", "register", "sign", "mail", "refill", "organize", "outline", "edit", "revise", "message", "ping", "confirm", "return", "visit", "renew", "rsvp"])

AREA_KEYWORDS = [
    ("gta", [r"\bis[ -]?640\b", r"\bmsc[ -]?550\b", r"\bis[ -]?401\b", r"\bis[ -]?501\b", r"\bgrad(e|ing)\b", r"\bgta\b",
             r"\boffice hours\b", r"\bta\b"]),
    ("school", [r"\bmkt[ -]?601\b", r"marketing strategy", r"managerial economics", r"engineering management",
                r"\bhomework\b", r"\bassignment\b", r"\bexam\b", r"\bquiz\b", r"\bpaper\b", r"\bclass\b"]),
    ("business", [r"\biflex\b", r"\brumble\b", r"\blead\b", r"\bsales\b", r"\binvoice\b", r"\bprospect\b"]),
    ("coaching", [r"\bclients?\b", r"\bcheck-?in\b", r"\bprogram for\b", r"\bpersonal training\b"]),
    ("faith", [r"\bccm\b", r"\bmass\b", r"\brosary\b", r"\bconfession\b", r"\bchurch\b", r"\bpray"]),
    ("training", [r"\bworkout\b", r"\bgym\b", r"\blift\b", r"\bwater\b", r"\bprogress photo\b", r"\bmeal prep\b"]),
    ("ai_guru", [r"\bai guru\b", r"\bstratosphere\b"]),
    ("admin", [r"\bgroceries\b", r"\bbill\b", r"\blaundry\b", r"\bclean\b", r"\bpick up\b", r"\bbuy\b", r"\bdentist\b",
               r"\bdoctor\b", r"\berrand"]),
]

ORDINALS = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2,
            "fourth": 3, "4th": 3, "four": 3, "fifth": 4, "5th": 4, "five": 4, "last": -1}


@dataclass
class Commitment:
    text: str                      # original clause
    content: str                   # cleaned task text
    when: timeparse.When
    area: str | None
    kind: str = "task"             # task | event
    duplicate_hint: bool = False   # user signalled intentional repeat ("another", "again")


@dataclass
class Interpretation:
    intent: str
    commitments: list[Commitment] = field(default_factory=list)
    item_refs: list[int] = field(default_factory=list)   # 0-based; -1 == last
    ref_text: str | None = None                            # content reference ("the groceries one")
    new_when: timeparse.When | None = None                 # for corrections
    old_phrase: str | None = None                          # "change Thursday to Friday" -> "thursday"
    approve_all: bool = False
    payload: str = ""                                       # remember/search text etc.
    confidence: str = "high"
    notes: list[str] = field(default_factory=list)


def _strip_leads(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = _LEAD_RE.sub("", s, count=1)
    return s.strip(" ,.;")


def detect_area(text: str) -> str | None:
    low = text.lower()
    for area, pats in AREA_KEYWORDS:
        if any(re.search(p, low) for p in pats):
            return area
    return None


def _first_word(s: str) -> str:
    m = re.match(r"\s*([A-Za-z']+)", _strip_leads(s))
    return m.group(1).lower() if m else ""


WAKE_RE = re.compile(r"(?:^|(?<=[.!?]\s))\s*(?:(?:yo|hey|hi|hello|ok(?:ay)?|alright|so|um+|uh+)[\s,]+)*jarvis\b[\s,.:!?-]*",
                     re.IGNORECASE)
# Phrases that begin a new commitment even in the middle of a sentence ("... and then remind me to ...").
_MID_LEAD = (r"(?:(?:can|could|would|will) you\s+|please\s+|i (?:want|need) you to\s+|i'd like you to\s+)?"
             r"(?:remind me to|i (?:also )?need to|i have to|i've got to|i got to|i gotta|don't let me forget to|"
             r"don't forget to|make sure (?:i|to)|set a reminder to|add a (?:task|to-?do) to)\b")
_SPLIT_BEFORE_LEAD = re.compile(r"\s*,?\s+(?:(?:and|also|plus)\s+)?(?:then\s+)?(?=" + _MID_LEAD + ")", re.IGNORECASE)


def strip_wake(text: str) -> str:
    """Remove "Hey/Yo/OK Jarvis," style addressing — it's never a task."""
    return WAKE_RE.sub("", text).strip()


def split_commitments(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", strip_wake(text.strip()))
    parts = re.split(r"\s*;\s*|(?<=[.!?])\s+(?=[A-Za-z])", t)
    out: list[str] = []
    for p in parts:
        p = strip_wake(p)
        for q in _SPLIT_BEFORE_LEAD.split(p):
            # "then" / "after that" always starts a new commitment; ", " / " and " only when the right side does.
            chunks = re.split(r"(,?\s+(?:and\s+)?then\s+|,?\s+after that,?\s+|,\s*(?:and\s+)?|\s+and\s+|\s+also\s+)", q)
            cur = chunks[0]
            i = 1
            while i < len(chunks):
                sep, nxt = chunks[i], chunks[i + 1] if i + 1 < len(chunks) else ""
                always = bool(re.search(r"\bthen\b|after that", sep, re.IGNORECASE)) and len(nxt.split()) >= 2
                starts_new = always or _first_word(nxt) in VERBS
                if starts_new:
                    out.append(cur)
                    cur = nxt
                else:
                    cur = cur + sep + nxt
                i += 2
            out.append(cur)
    cleaned = [re.sub(r"[,\s]+(?:please|thanks?|thank you)\s*$", "", c.strip(" ,.;!?"), flags=re.I).strip(" ,.;!?")
               for c in out]
    return [c for c in cleaned if c and not re.fullmatch(r"(?:thanks?|thank you|please|jarvis|ok(?:ay)?|that's (?:it|all))", c, re.I)]


def extract_commitments(text: str, captured_at: datetime, tz: str) -> list[Commitment]:
    res = []
    for clause in split_commitments(text):
        body = _strip_leads(clause)
        if not body or len(body) < 3:
            continue
        w = timeparse.resolve(body, captured_at, tz)
        content = timeparse.strip_when(body, w)
        if w.duration_min:
            content = re.sub(r"\b(for )?\d{1,3}\s*(minutes?|mins?|hours?|hrs?)\b", "", content, flags=re.IGNORECASE).strip()
        content = re.sub(r"\s{2,}", " ", content).strip(" ,.;")
        if not content:
            continue
        if len(content.split()) == 1 and content.lower() in VERBS:
            continue  # dangling fragment ("I need to grade") — nothing actionable to propose; never guessed
        content = content[0].upper() + content[1:]
        rep = bool(re.search(r"\b(another|again|second|one more)\b", clause, re.IGNORECASE))
        res.append(Commitment(text=clause, content=content, when=w, area=detect_area(clause), duplicate_hint=rep))
    return res


APPROVE_RE = re.compile(r"^\s*(?:yes[, ]+)?(?:approve(?:d)?|confirm(?:ed)?|go ahead|do it|execute(?: it)?|make it so|"
                        r"yes,? do it|yes,? approve)\b(.*)$", re.IGNORECASE)
VAGUE_OK_RE = re.compile(r"^\s*(yes|yeah|yep|ok(ay)?|sure|sounds good|looks good|perfect|great|cool|fine)[.! ]*$", re.IGNORECASE)
CANCEL_RE = re.compile(r"^\s*(cancel|never ?mind|scratch that|forget it|stop|abort|don't do (it|that))( that| it| this| the plan)?"
                       r"[.! ]*$", re.IGNORECASE)


def _parse_refs(s: str) -> tuple[list[int], bool]:
    low = s.lower()
    if re.search(r"\b(all|everything|all of (them|it)|them all)\b", low):
        return [], True
    refs = []
    for m in re.finditer(r"\b(?:item|number|#|no\.?)\s*(\d{1,2})\b", low):
        refs.append(int(m.group(1)) - 1)
    for m in re.finditer(r"\b(\d{1,2})\b", low):
        if not re.search(r"(am|pm|:|minutes?|hours?)", low[m.end():m.end() + 8]):
            v = int(m.group(1)) - 1
            if 0 <= v < 20 and v not in refs:
                refs.append(v)
    for word, idx in ORDINALS.items():
        if re.search(rf"\b(the )?{word}( one| item| task)?\b", low) and word not in ("one",):
            if idx not in refs:
                refs.append(idx)
    if re.search(r"\bfirst two\b", low):
        refs = [0, 1]
    return refs, False


def classify(text: str, *, has_active_proposal: bool, has_attachments: bool, captured_at: datetime, tz: str) -> Interpretation:
    t = text.strip()
    low = t.lower().strip()

    if CANCEL_RE.match(t):
        return Interpretation("cancel")

    m = APPROVE_RE.match(t)
    if m:
        refs, all_ = _parse_refs(m.group(1) or "")
        return Interpretation("approve", item_refs=refs, approve_all=all_ or not refs)
    if has_active_proposal and VAGUE_OK_RE.match(t):
        return Interpretation("vague_consent", confidence="low")

    if re.search(r"\bwhat (did|have) you (just )?(add|added|create|created|do|done|schedule|scheduled|put)\b|"
                 r"\bwhat happened( to| with)?\b.*\b(task|event|plan|that)\b|\bwhat did we (add|do)\b", low):
        return Interpretation("retrieve_actions")

    if has_active_proposal:
        corr = _correction(t, captured_at, tz)
        if corr:
            return corr

    if re.match(r"^(please )?(remember|note|keep in mind)( that)?\b", low):
        payload = re.sub(r"^(please )?(remember|note|keep in mind)( that)?\s*", "", t, flags=re.IGNORECASE)
        return Interpretation("remember", payload=payload)
    if re.match(r"^(please )?forget (that|the (memory|note))\b", low):
        return Interpretation("forget_memory", payload=re.sub(r"^(please )?forget (that )?", "", t, flags=re.IGNORECASE))

    if re.search(r"\b(debrief|action items|follow-?ups?|decisions)\b.*\b(meeting|notes|transcript|call)\b|"
                 r"\bmeeting (debrief|notes|recap)\b", low):
        return Interpretation("meeting_debrief", payload=t)
    if re.search(r"\b(monk mode|operation cruise|training plan|workout plan|my training|nutrition targets?)\b", low):
        return Interpretation("area_summary", payload="training")
    if re.search(r"\b(iflex|rumble|sales follow-?ups?|business next|my business|clients?)\b", low) and \
            (t.endswith("?") or re.match(r"^(what|how|where|show|list)\b", low)):
        return Interpretation("area_summary", payload="coaching" if "client" in low else "business")
    if re.search(r"\b(nightly debrief|debrief me|end of day|evening debrief)\b", low):
        return Interpretation("debrief")
    if re.search(r"\b(war room|weekly review|week in review|plan (my|the) week)\b", low):
        return Interpretation("war_room")
    if re.search(r"\b(brief me|daily command|morning brief(ing)?|what'?s (on )?my day|what should i (do|focus on|work on)"
                 r"( today| first| now)?|what'?s the plan( today)?|today'?s mission)\b", low) and "gta" not in low:
        return Interpretation("brief")
    if re.search(r"\b(during|in|at) (my )?(gta|ta) (shift|hours|window)\b|\bgta shift\b", low):
        return Interpretation("gta_queue")

    if re.search(r"\b(list|show)( me)? (the )?files\b|\bread (the )?file\b|\b(create|write|save) (a |the )?(note|report)\b|"
                 r"\b(save|export) (this|that|the|today'?s) (briefing|brief|action list|memory|memories)\b|\brename\b.*\bto\b|"
                 r"\bmove (the )?file\b|\bsearch (my )?(workspace|files)\b", low):
        return Interpretation("local_file", payload=t)

    if has_attachments or re.search(r"\b(screenshot|image|picture|photo|pdf|document|doc|slide|diagram|attached|this file)\b", low) \
            and re.search(r"\?|\bwhat\b|\bwhich\b|\bwhen\b|\bsummar|\bexplain|\bread\b", low):
        return Interpretation("knowledge", payload=t)
    if re.search(r"\b(search|look up|find) (my )?(notes|docs|documents|drive|knowledge|sources)\b|\baccording to\b|"
                 r"\bwhat do my notes say\b", low):
        return Interpretation("knowledge", payload=t)

    if re.search(r"\b(block|schedule|book|find|carve out|reserve|put)\b.*\b(time|slot|minutes?|hours?|calendar|session|block)\b",
                 low) or re.search(r"\bon (my )?calendar\b", low):
        cs = extract_commitments(t, captured_at, tz)
        for c in cs:
            c.kind = "event"
        return Interpretation("schedule", commitments=cs, payload=t)

    if re.search(r"^(mark|check off|complete|close)\b|\b(i )?(finished|completed|am done with|'m done with)\b|\bmark .* (as )?(done|complete)",
                 low):
        return Interpretation("complete", payload=t)

    if re.search(r"\b(summari[sz]e|recap)\b", low):
        return Interpretation("summarize", payload=t)

    cs = extract_commitments(t, captured_at, tz)
    is_question = t.endswith("?") or re.match(r"^(what|when|where|who|why|how|which|is|are|do|does|can|should|will|am)\b", low)
    capture_signal = re.match(r"^\s*(i (also )?(need|have|should|must|gotta)|remind me|add|todo|to-do|don't let me forget|i've got)",
                              low) or (_first_word(t) in VERBS)
    if cs and capture_signal and not is_question:
        return Interpretation("capture", commitments=cs)
    if is_question:
        return Interpretation("question", payload=t)
    if cs and any(c.when.date for c in cs):
        return Interpretation("capture", commitments=cs, confidence="medium")
    return Interpretation("question", payload=t, confidence="low")


def _correction(t: str, captured_at: datetime, tz: str) -> Interpretation | None:
    low = t.lower()
    # remove / drop an item
    m = re.match(r"^\s*(?:actually,?\s*)?(remove|drop|delete|skip|take out|forget)\s+(.*)$", low)
    if m and not re.search(r"\b(task|event) (in|from) todoist\b", low):
        refs, _ = _parse_refs(m.group(2))
        return Interpretation("remove_item", item_refs=refs, ref_text=m.group(2).strip(" .!"))
    # "change Thursday to Friday", "Friday not Thursday", "make that Friday", "move the second one to Friday"
    m = re.search(r"\b(?:change|switch|move)\s+(.+?)\s+to\s+(.+)$", low) or re.search(r"\bmake (that|it|the .+?) (.+)$", low) \
        or re.search(r"^(?:actually,?\s*|no,?\s*)(.+)$", low)
    if not m:
        return None
    target_part, new_part = (m.group(1), m.group(2)) if m.lastindex and m.lastindex >= 2 else ("that", m.group(1))
    new_when = timeparse.resolve(new_part, captured_at, tz)
    not_m = re.search(r"(.+?),?\s+not\s+(.+)$", new_part)
    old_phrase = None
    if not_m:
        new_when = timeparse.resolve(not_m.group(1), captured_at, tz)
        old_phrase = not_m.group(2).strip(" .!")
    if not (new_when.date or new_when.time or new_when.duration_min):
        return None
    refs, _ = _parse_refs(target_part)
    tp_low = target_part.strip()
    if timeparse.resolve(tp_low, captured_at, tz).date and not refs:
        old_phrase = old_phrase or tp_low
    ref_text = None if tp_low in ("that", "it", "this", "that one", "it to") else tp_low
    return Interpretation("correct", item_refs=refs, new_when=new_when, old_phrase=old_phrase, ref_text=ref_text,
                          payload=t)
