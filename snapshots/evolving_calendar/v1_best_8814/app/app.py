#!/usr/bin/env python3
"""Evolving Calendar (Iris build): single-file, rule-based planning server. Standard library only."""

import argparse
import json
import os
import re
import threading
from datetime import datetime, date, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

# ----------------------------------------------------------------- constants
FMT = "%Y-%m-%dT%H:%M"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = None
LOCK = threading.Lock()
GREETING = "Hi, I'm here whenever you want to plan a little."
DEFAULT_HOUR = 19
FOCUS_ACTION = "Start 5 minutes"
ITEM_KINDS = ("task", "event", "note")

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot", "lazy", "urgent", "alert"]
KIND_WORDS = {
    "overdue": "waiting",
    "fail": "not work out",
    "miss": "skip",
    "late": "after the planned time",
    "behind": "still on the list",
    "should have": "could",
    "forgot": "did not get to",
    "lazy": "tired",
    "urgent": "soon",
    "alert": "note",
}

WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_MAP = {}
for _i, _name in enumerate(WEEKDAY_NAMES):
    WEEKDAY_MAP[_name] = _i
    WEEKDAY_MAP[_name[:3]] = _i
WEEKDAY_MAP.update({"tues": 1, "thur": 3, "thurs": 3})

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
REL_RE = re.compile(
    r"\b(?:(?:on|next|this)\s+)?(today|tomorrow|"
    + "|".join(sorted(WEEKDAY_MAP, key=len, reverse=True))
    + r")\b\.?",
    re.IGNORECASE,
)
AMPM_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?(?![a-z])", re.IGNORECASE)
COLON_RE = re.compile(r"(?:\bat\s+)?(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])", re.IGNORECASE)
AT_RE = re.compile(r"\bat\s+(\d{1,2})\b", re.IGNORECASE)
APPT_RE = re.compile(r"\b(with|meet|meeting|lunch|dinner)\b", re.IGNORECASE)
NO_RE = re.compile(r"\bno\b|\bnope\b|not now|not right now|no thanks|\blater\b", re.IGNORECASE)
YES_RE = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|please|great|perfect)\b|looks good|sounds good|do it|go ahead",
    re.IGNORECASE,
)
CHANGE_RE = re.compile(r"smaller|shorter|change|different|too big|too much|split|bigger|\bless\b", re.IGNORECASE)

# (regex, start hour, end hour); lunch and dinner stay in the title
WINDOW_RULES = [
    (re.compile(r"\bthis afternoon\b", re.IGNORECASE), 14, 17),
    (re.compile(r"\baround noon\b", re.IGNORECASE), 12, 13),
    (re.compile(r"\bnoon\b", re.IGNORECASE), 12, 13),
    (re.compile(r"\bthis morning\b|\bmorning\b", re.IGNORECASE), 9, 12),
    (re.compile(r"\bafternoon\b", re.IGNORECASE), 14, 17),
    (re.compile(r"\b(?:this )?evening\b", re.IGNORECASE), 18, 21),
    (re.compile(r"\btonight\b", re.IGNORECASE), 20, 22),
    (re.compile(r"\blunch\b", re.IGNORECASE), 12, 13),
    (re.compile(r"\bdinner\b", re.IGNORECASE), 18, 20),
]
KEEP_IN_TITLE = ("lunch", "dinner")

BIG_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"\bwork on\b", r"\bfigure out\b", r"\bdeal with\b", r"\bsort out\b",
    r"\bget\b.*\btogether\b", r"\borgani[sz]e\b", r"\bplan\b", r"\bprepare\b",
    r"\bproject\b", r"\bportfolio\b", r"\bpitch deck\b", r"\bthesis\b",
    r"\bwebsite\b", r"\bgarage\b", r"\bmove\b", r"\btaxes\b",
)]

STEP_TEMPLATES = [
    (r"portfolio", [
        ("List the projects you are proudest of", 10),
        ("Pick the five best projects", 30),
        ("Write a short note for each project", 60),
        ("Upload the projects to a simple website", 60),
    ]),
    (r"pitch", [
        ("List the three points your pitch must make", 10),
        ("Pick the one story to open with", 20),
        ("Draft the slides as a simple outline", 45),
        ("Review it and send it to a friend", 30),
    ]),
    (r"thesis", [
        ("List the questions your thesis answers", 10),
        ("Pick the chapter you can draft first", 20),
        ("Draft one page of that chapter", 45),
        ("Send the page to your advisor", 15),
    ]),
    (r"garage", [
        ("List what is in the garage", 10),
        ("Pick one shelf to clear first", 20),
        ("Clear that shelf into keep, give and bin piles", 45),
        ("Email a friend about the give pile", 30),
    ]),
    (r"taxes", [
        ("Gather last year's tax papers in one folder", 10),
        ("Pick the forms you already received", 20),
        ("Sort the receipts by month", 45),
        ("Review the list and note any gaps", 30),
    ]),
]

# ----------------------------------------------------------------- state
S = {
    "now": datetime(2026, 10, 19, 9, 0),
    "items": [],
    "messages": [],
    "convo": {},
    "undo": {},
    "sent": set(),
    "checked": set(),
    "plans": {},
    "answers": {},
    "seq": 0,
}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def new_id(prefix):
    S["seq"] += 1
    return f"{prefix}{S['seq']}"


def parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in (FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fmt_iso(dt):
    return dt.strftime(FMT) if dt is not None else None


def new_messages(start_idx):
    return list(S["messages"][start_idx:])


def sanitize(text):
    out = str(text)
    for word in BANNED:
        out = re.sub(re.escape(word), KIND_WORDS[word], out, flags=re.IGNORECASE)
    return out


def add_agent(kind, text, item_id=None, undo_id=None, steps=None):
    msg = {
        "id": new_id("m"),
        "role": "agent",
        "kind": kind,
        "text": sanitize(text),
        "item_id": item_id,
        "at": fmt_iso(S["now"]),
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        msg["steps"] = [
            {"title": sanitize(s["title"]), "estimate_min": int(s["estimate_min"])}
            for s in (steps or [])
        ]
    S["messages"].append(msg)
    return msg


def add_person(text, item_id=None):
    msg = {
        "id": new_id("m"),
        "role": "person",
        "kind": "reply",
        "text": str(text),
        "item_id": item_id,
        "at": fmt_iso(S["now"]),
        "undo_id": None,
    }
    S["messages"].append(msg)
    return msg


def make_item(title, kind="task", start=None, end=None, window=None, why=None, parent_id=None,
              estimate_min=None, source="typed", status="open", item_id=None, postpone_count=0):
    return {
        "id": item_id or new_id("it"),
        "kind": kind,
        "title": title,
        "start": start,
        "end": end,
        "window": window,
        "status": status,
        "why": why,
        "parent_id": parent_id,
        "estimate_min": estimate_min,
        "postpone_count": postpone_count,
        "source": source,
    }


def normalize_seed(raw):
    if not isinstance(raw, dict):
        raise ApiError(400, "items must be JSON objects")
    kind = raw.get("kind") if raw.get("kind") in ITEM_KINDS else "task"
    status = "done" if raw.get("status") == "done" else "open"
    try:
        estimate = int(raw["estimate_min"]) if raw.get("estimate_min") is not None else None
    except (TypeError, ValueError):
        estimate = None
    try:
        postpone = int(raw.get("postpone_count") or 0)
    except (TypeError, ValueError):
        postpone = 0
    return make_item(
        str(raw.get("title") or "Untitled"),
        kind=kind,
        start=fmt_iso(parse_iso(raw.get("start"))),
        end=fmt_iso(parse_iso(raw.get("end"))),
        window=raw.get("window"),
        why=raw.get("why"),
        parent_id=raw.get("parent_id"),
        estimate_min=estimate,
        source=raw.get("source") or "seed",
        status=status,
        item_id=str(raw.get("id") or new_id("seed")),
        postpone_count=postpone,
    )


def find_item(item_id):
    for it in S["items"]:
        if it["id"] == item_id:
            return it
    return None


def has_open_children(item_id):
    return any(i.get("parent_id") == item_id and i["status"] == "open" for i in S["items"])


def set_state(item_id, state):
    if state is None:
        S["convo"].pop(item_id, None)
    else:
        S["convo"][item_id] = state


def is_no(text):
    return bool(NO_RE.search(text or ""))


def is_yes(text):
    return bool(YES_RE.search(text or ""))


# ----------------------------------------------------------------- parsing
def date_from_match(regex, m, today_d):
    if regex is ISO_RE:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
        except ValueError:
            return None, m.group(0)
    if regex is MONTH_DAY_RE:
        try:
            return date(today_d.year, MONTHS[m.group(1).lower()], int(m.group(2))), m.group(0)
        except ValueError:
            return None, m.group(0)
    word = m.group(1).lower()
    if word == "today":
        return today_d, word
    if word == "tomorrow":
        return today_d + timedelta(days=1), word
    days = (WEEKDAY_MAP[word] - today_d.weekday()) % 7 or 7
    return today_d + timedelta(days=days), word


def extract_date(text, now_dt):
    today_d = now_dt.date()
    found = None
    phrase = None
    out = text or ""
    for regex in (ISO_RE, MONTH_DAY_RE, REL_RE):
        m = regex.search(out)
        while m:
            value, word = date_from_match(regex, m, today_d)
            if found is None and value is not None:
                found, phrase = value, word
            out = out[:m.start()] + " " + out[m.end():]
            m = regex.search(out)
    return found, out, phrase


def clock_from_parts(hour, minute, ampm):
    h = int(hour)
    mi = int(minute) if minute else 0
    if mi > 59:
        return None
    if ampm:
        if h < 1 or h > 12:
            return None
        if ampm.lower() == "p" and h < 12:
            h += 12
        elif ampm.lower() == "a" and h == 12:
            h = 0
    elif 1 <= h <= 7:
        h += 12
    if h > 23:
        return None
    return dtime(h, mi)


def extract_clock(text):
    out = text or ""
    clock = None
    phrase = None
    for regex, kind in ((AMPM_RE, "ampm"), (COLON_RE, "colon"), (AT_RE, "at")):
        m = regex.search(out)
        while m:
            if kind == "ampm":
                value = clock_from_parts(m.group(1), m.group(2), m.group(3))
            elif kind == "colon":
                value = clock_from_parts(m.group(1), m.group(2), None)
            else:
                value = clock_from_parts(m.group(1), None, None)
            if clock is None and value is not None:
                clock, phrase = value, m.group(0).strip()
            out = out[:m.start()] + " " + out[m.end():]
            m = regex.search(out)
    return clock, out, phrase


def extract_window(text):
    for regex, sh, eh in WINDOW_RULES:
        m = regex.search(text or "")
        if m:
            word = m.group(0).lower()
            if word in KEEP_IN_TITLE:
                out = text
            else:
                out = text[:m.start()] + " " + text[m.end():]
            return word, sh, eh, out
    return None, None, None, text


def clean_title(text):
    t = re.sub(r"\bmaybe\b", " ", text or "", flags=re.IGNORECASE)
    t = t.replace("?", " ")
    t = re.sub(r"\s+", " ", t).strip(" ,;:.!-")
    t = re.sub(r"\s+(at|on|around)$", "", t, flags=re.IGNORECASE)
    if not t:
        return "Untitled"
    return t[0].upper() + t[1:]


def parse_fragment(raw, now_dt):
    text = " ".join(str(raw).split())
    today_d = now_dt.date()
    maybe = bool(re.search(r"\bmaybe\b", text, re.IGNORECASE)) or text.endswith("?")
    day_d, rest, date_word = extract_date(text, now_dt)
    clock, rest, _ = extract_clock(rest)
    window_word, sh, eh, rest = extract_window(rest)
    title = clean_title(rest)
    appointment = bool(APPT_RE.search(text))

    if clock is not None:
        if day_d is not None:
            base_d = day_d
        elif datetime.combine(today_d, clock) > now_dt:
            base_d = today_d
        else:
            base_d = today_d + timedelta(days=1)
        start_dt = datetime.combine(base_d, clock)
        return {
            "title": title,
            "kind": "note" if maybe else "event",
            "start": fmt_iso(start_dt),
            "end": fmt_iso(start_dt + timedelta(minutes=60)),
            "window": None,
        }

    if window_word is not None:
        if day_d is not None:
            base_d = day_d
        elif datetime.combine(today_d, dtime(sh, 0)) > now_dt:
            base_d = today_d
        else:
            base_d = today_d + timedelta(days=1)
        start_dt = datetime.combine(base_d, dtime(sh, 0))
        end_dt = datetime.combine(base_d, dtime(eh, 0))
        if maybe:
            kind = "note"
        else:
            kind = "event" if appointment else "task"
        return {
            "title": title,
            "kind": kind,
            "start": fmt_iso(start_dt),
            "end": fmt_iso(end_dt),
            "window": window_word,
        }

    kind = "note" if maybe else "task"
    window = date_word if day_d is not None else None
    return {"title": title, "kind": kind, "start": None, "end": None, "window": window}


def create_item_from_text(raw, source, now_dt):
    parsed = parse_fragment(raw, now_dt)
    item = make_item(
        parsed["title"],
        kind=parsed["kind"],
        start=parsed["start"],
        end=parsed["end"],
        window=parsed["window"],
        source=source,
    )
    S["items"].append(item)
    return item


def is_big(item):
    if (item.get("estimate_min") or 0) > 120:
        return True
    title = item.get("title") or ""
    return any(r.search(title) for r in BIG_RES)


def describe_when(item):
    st = parse_iso(item.get("start"))
    if st is not None and item.get("window"):
        return f"for {item['window']} on {st:%a %b %d}"
    if st is not None:
        return f"on {st:%a %b %d} at {st:%H:%M}"
    if item.get("window"):
        return f"for {item['window']}"
    return "to your list"


def ask_split_question(item):
    add_agent(
        "question",
        f"This one sounds big. Want me to break '{item['title']}' into small steps?",
        item_id=item["id"],
    )
    set_state(item["id"], "ask_split")


# ----------------------------------------------------------------- plans
def build_steps(title):
    low = (title or "").lower()
    template = None
    for pattern, steps in STEP_TEMPLATES:
        if re.search(pattern, low):
            template = steps
            break
    if template is None:
        template = [
            (f"Open a note and list what '{title}' needs", 10),
            ("Pick the first piece to start", 20),
            ("Draft the first piece", 45),
            ("Review it and send or save it", 30),
        ]
    return [{"title": t, "estimate_min": e} for t, e in template]


def revise_steps(title):
    base = build_steps(title)
    steps = [{"title": f"Open a note for '{title}'", "estimate_min": 10}]
    for s in base[:3]:
        steps.append({"title": s["title"], "estimate_min": min(s["estimate_min"], 30)})
    return steps[:5]


def send_breakdown(item, steps, deadline_d, text=None, state="propose"):
    S["plans"][item["id"]] = {"steps": steps, "deadline": deadline_d}
    set_state(item["id"], state)
    msg_text = text or f"Here's a gentle plan for '{item['title']}'. Does this look ok?"
    add_agent("breakdown", msg_text, item_id=item["id"], steps=steps)


def usual_hour():
    minutes = []
    for it in S["items"]:
        if it["status"] == "done" and it.get("source") == "seed":
            st = parse_iso(it.get("start"))
            if st is not None:
                minutes.append(st.hour * 60 + st.minute)
    if not minutes:
        return DEFAULT_HOUR
    minutes.sort()
    median = minutes[(len(minutes) - 1) // 2]
    return median // 60


def item_minutes(item):
    s = parse_iso(item.get("start"))
    e = parse_iso(item.get("end"))
    if s is not None and e is not None and e > s:
        return int((e - s).total_seconds() // 60)
    return int(item.get("estimate_min") or 30)


def is_free(start_dt, end_dt, exclude_id=None):
    for it in S["items"]:
        if it["status"] != "open" or it["id"] == exclude_id:
            continue
        s = parse_iso(it.get("start"))
        if s is None:
            continue
        e = parse_iso(it.get("end")) or (s + timedelta(minutes=1))
        if s < end_dt and start_dt < e:
            return False
    return True


def find_free_start(start_dt, minutes, exclude_id=None):
    candidate = start_dt
    limit = datetime.combine(candidate.date(), dtime(23, 30))
    while candidate <= limit:
        if is_free(candidate, candidate + timedelta(minutes=minutes), exclude_id):
            return candidate
        candidate += timedelta(minutes=15)
    return start_dt


def find_slot_on_day(day_d, minutes, usual, exclude_id=None):
    limit = datetime.combine(day_d, dtime(23, 30))
    cand = datetime.combine(day_d, dtime(usual, 0))
    while cand <= limit:
        if is_free(cand, cand + timedelta(minutes=minutes), exclude_id):
            return cand
        cand += timedelta(minutes=15)
    cand = datetime.combine(day_d, dtime(8, 0))
    while cand <= limit:
        if is_free(cand, cand + timedelta(minutes=minutes), exclude_id):
            return cand
        cand += timedelta(minutes=15)
    return datetime.combine(day_d, dtime(usual, 0))


def schedule_steps(goal, steps, deadline_d):
    now_dt = S["now"]
    today_d = now_dt.date()
    usual = usual_hour()
    if datetime.combine(today_d, dtime(usual, 0)) > now_dt:
        first_day = today_d
    else:
        first_day = today_d + timedelta(days=1)
    last_ok = deadline_d - timedelta(days=1)
    if last_ok < first_day:
        last_ok = first_day
    created = []
    cursor = {}
    for index, step in enumerate(steps):
        day_d = min(first_day + timedelta(days=index), last_ok)
        est = int(step.get("estimate_min") or 30)
        start_dt = cursor.get(day_d) or datetime.combine(day_d, dtime(usual, 0))
        start_dt = find_free_start(start_dt, est)
        end_dt = start_dt + timedelta(minutes=est)
        item = make_item(
            step["title"],
            kind="task",
            start=fmt_iso(start_dt),
            end=fmt_iso(end_dt),
            parent_id=goal["id"],
            estimate_min=est,
            source="agent",
        )
        S["items"].append(item)
        created.append(item)
        cursor[day_d] = end_dt
    return created


def accept_plan(item):
    plan = S["plans"].get(item["id"]) or {}
    steps = plan.get("steps") or build_steps(item["title"])
    deadline_d = plan.get("deadline") or (S["now"].date() + timedelta(days=7))
    schedule_steps(item, steps, deadline_d)
    set_state(item["id"], None)
    first_title = steps[0]["title"] if steps else item["title"]
    add_agent("info", f"Done, the first tiny step is '{first_title}'.", item_id=item["id"])


def deadline_from_reply(text):
    day_d, _, _ = extract_date(text or "", S["now"])
    if day_d is not None:
        return day_d
    return S["now"].date() + timedelta(days=7)


# ----------------------------------------------------------------- commands
def handle_command(text, mode):
    start_idx = len(S["messages"])
    now_dt = S["now"]
    clean = (text or "").strip()
    created = []
    if not clean:
        add_agent("info", "I'm here. Tell me one thing you'd like to plan, or a few separated by commas.")
        return {"messages": new_messages(start_idx), "created": []}
    if mode == "dump":
        fragments = [f.strip() for f in re.split(r"[,;\n]+", clean) if f.strip()]
        if not fragments:
            fragments = [clean]
        created = [create_item_from_text(f, "dump", now_dt) for f in fragments]
        titles = ", ".join(it["title"] for it in created)
        add_agent("info", f"I added {len(created)} things to your list: {titles}.")
        for it in created:
            if it["kind"] == "task" and is_big(it):
                ask_split_question(it)
                break
    else:
        item = create_item_from_text(clean, "typed", now_dt)
        created = [item]
        add_agent("info", f"Added '{item['title']}' {describe_when(item)}.", item_id=item["id"])
        if item["kind"] == "task" and is_big(item):
            ask_split_question(item)
    return {"messages": new_messages(start_idx), "created": [it["id"] for it in created]}


# ----------------------------------------------------------------- chat
def chat_idle(item, text):
    add_agent(
        "info",
        f"I'm keeping '{item['title']}' as it is for now. Tell me what you'd like to add or change next.",
        item_id=item["id"],
    )


def chat_ask_split(item, text):
    if is_no(text):
        set_state(item["id"], None)
        add_agent("info", "Okay, I'll keep it as one task.", item_id=item["id"])
        return
    set_state(item["id"], "ask_why")
    add_agent("question", "Why does this matter to you?", item_id=item["id"])


def chat_ask_why(item, text):
    item["why"] = (text or "").strip()
    set_state(item["id"], "ask_done")
    add_agent("question", "What does done look like for you?", item_id=item["id"])


def chat_ask_done(item, text):
    S["answers"][item["id"]] = (text or "").strip()
    set_state(item["id"], "ask_deadline")
    add_agent("question", "Is there a deadline, a day you'd like it finished by?", item_id=item["id"])


def chat_ask_deadline(item, text):
    deadline_d = deadline_from_reply(text)
    send_breakdown(item, build_steps(item["title"]), deadline_d)


def chat_propose(item, text):
    accepted = is_yes(text) and not is_no(text) and not CHANGE_RE.search(text or "")
    if accepted:
        accept_plan(item)
        return
    plan = S["plans"].get(item["id"]) or {}
    send_breakdown(
        item,
        revise_steps(item["title"]),
        plan.get("deadline"),
        text=f"Here's a smaller plan for '{item['title']}'. Does this look ok?",
    )


def record_undo(item):
    undo_id = new_id("undo")
    S["undo"][undo_id] = {
        "item_id": item["id"],
        "start": item.get("start"),
        "end": item.get("end"),
        "window": item.get("window"),
        "postpone_count": int(item.get("postpone_count") or 0),
    }
    return undo_id


def place_item(item, slot, minutes):
    item["start"] = fmt_iso(slot)
    item["end"] = fmt_iso(slot + timedelta(minutes=minutes))


def move_for_checkin(item):
    s = parse_iso(item.get("start"))
    base_d = s.date() if s is not None else S["now"].date()
    new_day = base_d + timedelta(days=1)
    minutes = item_minutes(item)
    undo_id = record_undo(item)
    slot = find_slot_on_day(new_day, minutes, usual_hour(), item["id"])
    place_item(item, slot, minutes)
    set_state(item["id"], None)
    add_agent(
        "info",
        f"Okay, I moved '{item['title']}' to {slot:%a %b %d} at {slot:%H:%M}. Undo if you'd rather keep it.",
        item_id=item["id"],
        undo_id=undo_id,
    )


def chat_checkin(item, text):
    low = (text or "").lower()
    if re.search(r"not needed|\bdrop\b|\bremove\b|don'?t need|do not need", low):
        set_state(item["id"], "confirm_drop")
        add_agent("question", f"Shall I remove '{item['title']}' for you?", item_id=item["id"])
        return
    if re.search(r"\bbig\b|smaller|\bbreak\b|too much", low):
        send_breakdown(item, build_steps(item["title"]), None)
        return
    if re.search(r"\btime\b|busy|wrong", low):
        move_for_checkin(item)
        return
    add_agent(
        "question",
        "Would a smaller step, a different time, or letting it go help more?",
        item_id=item["id"],
    )


def chat_confirm_drop(item, text):
    set_state(item["id"], None)
    if is_yes(text) and not is_no(text):
        S["items"][:] = [it for it in S["items"] if it["id"] != item["id"]]
        add_agent("info", f"Okay, I removed '{item['title']}'.")
    else:
        add_agent("info", f"Okay, I kept '{item['title']}'.", item_id=item["id"])


CHAT_HANDLERS = {
    "ask_split": chat_ask_split,
    "ask_why": chat_ask_why,
    "ask_done": chat_ask_done,
    "ask_deadline": chat_ask_deadline,
    "propose": chat_propose,
    "checkin": chat_checkin,
    "confirm_drop": chat_confirm_drop,
}


def handle_chat(item_id, text):
    item = find_item(item_id)
    if item is None:
        raise ApiError(404, "unknown item")
    start_idx = len(S["messages"])
    add_person(text, item_id=item_id)
    state = S["convo"].get(item_id)
    handler = CHAT_HANDLERS.get(state, chat_idle)
    handler(item, text)
    return new_messages(start_idx)


# ----------------------------------------------------------------- scheduler
def remind(item, part, text):
    key = (item["id"], part[0], part[1])
    if key in S["sent"]:
        return
    S["sent"].add(key)
    add_agent("reminder", text, item_id=item["id"])


def handle_postpone(item, t):
    end_dt = parse_iso(item.get("end"))
    if end_dt is None:
        return
    key = (item["id"], "post", fmt_iso(end_dt))
    if key in S["sent"]:
        return
    S["sent"].add(key)
    count = int(item.get("postpone_count") or 0)
    if count >= 2:
        if item["id"] in S["checked"]:
            return
        S["checked"].add(item["id"])
        set_state(item["id"], "checkin")
        add_agent(
            "checkin",
            f"How did '{item['title']}' feel: if it's too big I can break it into smaller steps, "
            f"find a better time, or let it go. What would help?",
            item_id=item["id"],
        )
        return
    minutes = item_minutes(item)
    new_day = end_dt.date() + timedelta(days=1)
    slot = find_slot_on_day(new_day, minutes, usual_hour(), item["id"])
    undo_id = record_undo(item)
    place_item(item, slot, minutes)
    item["postpone_count"] = count + 1
    add_agent(
        "postponed",
        f"No worries, I moved '{item['title']}' to {slot:%a %b %d} at {slot:%H:%M}. "
        f"Undo if you'd rather keep it.",
        item_id=item["id"],
        undo_id=undo_id,
    )


def process_minute(t):
    S["now"] = t
    for item in list(S["items"]):
        if item["status"] != "open":
            continue
        s = parse_iso(item.get("start"))
        e = parse_iso(item.get("end"))
        if s is not None and s == t:
            remind(item, ("start", fmt_iso(s)), f"It's time for '{item['title']}' ({s:%H:%M}).")
        if e is not None and e - timedelta(minutes=10) == t:
            remind(item, ("end", fmt_iso(e)), f"'{item['title']}' wraps up in 10 minutes.")
        if (e is not None and item["kind"] == "task" and e + timedelta(minutes=31) == t
                and not has_open_children(item["id"])):
            handle_postpone(item, t)


def clock_candidates(cur):
    out = []
    for it in S["items"]:
        if it["status"] != "open":
            continue
        s = parse_iso(it.get("start"))
        e = parse_iso(it.get("end"))
        if s is not None:
            out.append(s)
        if e is not None:
            out.append(e - timedelta(minutes=10))
            if it["kind"] == "task":
                out.append(e + timedelta(minutes=31))
    return out


def catch_up_postpones(now_dt):
    for item in list(S["items"]):
        if item["status"] != "open" or item["kind"] != "task":
            continue
        e = parse_iso(item.get("end"))
        if e is None or e + timedelta(minutes=31) > now_dt:
            continue
        if has_open_children(item["id"]):
            continue
        if (item["id"], "post", fmt_iso(e)) in S["sent"]:
            continue
        S["now"] = now_dt
        handle_postpone(item, now_dt)


def advance_clock(new_dt):
    start_idx = len(S["messages"])
    now_dt = S["now"]
    if new_dt <= now_dt:
        return []
    catch_up_postpones(now_dt)
    cur = now_dt
    while True:
        future = [c for c in clock_candidates(cur) if cur < c <= new_dt]
        if not future:
            break
        t = min(future)
        process_minute(t)
        cur = t
    S["now"] = new_dt
    return new_messages(start_idx)


# ----------------------------------------------------------------- focus
def min_by_start(candidates):
    best = None
    best_key = None
    for idx, it in enumerate(candidates):
        st = parse_iso(it.get("start"))
        key = (st is None, st if st is not None else datetime.max, idx)
        if best_key is None or key < best_key:
            best, best_key = it, key
    return best


def compute_focus():
    open_items = [it for it in S["items"] if it["status"] == "open"]
    goals = [it for it in open_items if it.get("why")]
    goal_ids = {g["id"] for g in goals}
    children = [it for it in open_items if it.get("parent_id") in goal_ids]
    if children:
        child = min_by_start(children)
        parent = find_item(child.get("parent_id"))
        why = parent.get("why") if parent else None
        return child, parent, why
    tasks = [it for it in open_items if it["kind"] == "task" and not has_open_children(it["id"])]
    if not tasks:
        return None, None, None
    task = min_by_start(tasks)
    parent = find_item(task["parent_id"]) if task.get("parent_id") else None
    why = task.get("why") or (parent.get("why") if parent else None)
    return task, parent, why


# ----------------------------------------------------------------- endpoints
def api_state():
    return {"now": fmt_iso(S["now"]), "items": S["items"], "messages": S["messages"]}


def api_reset(body):
    now_dt = parse_iso(body.get("now"))
    if now_dt is None:
        raise ApiError(400, "now must be an ISO time like 2026-10-19T09:00")
    raw_items = body.get("items") or []
    if not isinstance(raw_items, list):
        raise ApiError(400, "items must be a list")
    seeds = [normalize_seed(r) for r in raw_items]
    S["now"] = now_dt
    S["items"] = seeds
    S["messages"] = []
    S["convo"] = {}
    S["undo"] = {}
    S["sent"] = set()
    S["checked"] = set()
    S["plans"] = {}
    S["answers"] = {}
    add_agent("info", GREETING)
    return api_state()


def api_command(body):
    text = body.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    mode = body.get("mode") or "command"
    return handle_command(text, mode)


def api_chat(body):
    item_id = body.get("item_id")
    if not item_id:
        raise ApiError(400, "item_id is required")
    text = body.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    return {"messages": handle_chat(str(item_id), text)}


def api_done(item_id):
    item = find_item(item_id)
    if item is None:
        raise ApiError(404, "unknown item")
    item["status"] = "done"
    return item


def api_undo(body):
    undo_id = body.get("undo_id")
    rec = S["undo"].get(str(undo_id)) if undo_id else None
    if rec is None:
        raise ApiError(404, "unknown undo_id")
    item = find_item(rec["item_id"])
    if item is None:
        raise ApiError(404, "item no longer exists")
    item["start"] = rec["start"]
    item["end"] = rec["end"]
    item["window"] = rec["window"]
    item["postpone_count"] = rec["postpone_count"]
    return {"ok": True}


def api_clock(body):
    new_dt = parse_iso(body.get("now"))
    if new_dt is None:
        raise ApiError(400, "now must be an ISO time like 2026-10-19T20:00")
    return {"messages": advance_clock(new_dt)}


def api_focus():
    item, parent, why = compute_focus()
    return {"item": item, "parent": parent, "why": why, "action": FOCUS_ACTION}


def route_get(path):
    if path == "/health":
        return 200, {"ok": True}
    if path == "/api/state":
        return 200, api_state()
    if path == "/api/focus":
        return 200, api_focus()
    raise ApiError(404, "not found")


def route_post(path, body):
    if path == "/api/reset":
        return 200, api_reset(body)
    if path == "/api/command":
        return 200, api_command(body)
    if path == "/api/chat":
        return 200, api_chat(body)
    if path == "/api/undo":
        return 200, api_undo(body)
    if path == "/api/clock":
        return 200, api_clock(body)
    match = re.match(r"^/api/items/([^/]+)/done$", path)
    if match:
        return 200, {"item": api_done(unquote(match.group(1)))}
    raise ApiError(404, "not found")


def save_snapshot():
    if not DATA_DIR:
        return
    try:
        payload = {"now": fmt_iso(S["now"]), "items": S["items"], "messages": S["messages"]}
        with open(os.path.join(DATA_DIR, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:
        pass


BUILTIN_PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Evolving Calendar</title>
<style>
body{font-family:sans-serif;max-width:720px;margin:20px auto;padding:0 12px}
#chat-log{border:1px solid #ddd;height:280px;overflow:auto;padding:8px;margin-bottom:8px}
.agent{color:#234;margin:4px 0}.person{color:#432;text-align:right;margin:4px 0}
#focus{border:1px solid #9c9;padding:10px;margin:10px 0}
</style></head>
<body>
<h1>Evolving Calendar</h1>
<div id="focus">Loading focus...</div>
<div id="chat-log"></div>
<form id="chat-form"><input id="chat-input" size="60" placeholder="Tell me what is on your mind"> <button type="submit">Send</button></form>
<h3>Timeline</h3>
<ul id="timeline"></ul>
<script>
var logEl = document.getElementById('chat-log');
var input = document.getElementById('chat-input');
var focusEl = document.getElementById('focus');
var timeline = document.getElementById('timeline');
var activeItem = null;
var CHAT_KINDS = ['question', 'breakdown', 'checkin'];

function post(url, body) {
  return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
    .then(function (r) { return r.json(); });
}
function addLine(cls, text) {
  var d = document.createElement('div');
  d.className = cls;
  d.textContent = text;
  logEl.appendChild(d);
  logEl.scrollTop = logEl.scrollHeight;
}
function renderFocus(f) {
  focusEl.innerHTML = '';
  if (!f || !f.item) {
    focusEl.textContent = 'Nothing tiny is waiting right now. Enjoy the space.';
    return;
  }
  var t = document.createElement('div');
  t.textContent = f.item.title + (f.why ? ' - because: ' + f.why : '');
  var b = document.createElement('button');
  b.textContent = f.action || 'Start 5 minutes';
  b.onclick = function () { addLine('agent', 'Five minutes on ' + f.item.title + '. Just begin.'); };
  focusEl.appendChild(t);
  focusEl.appendChild(b);
}
function renderTimeline(items) {
  timeline.innerHTML = '';
  items.filter(function (it) { return it.status === 'open'; })
    .sort(function (a, b) { return (a.start || 'zzzz').localeCompare(b.start || 'zzzz'); })
    .forEach(function (it) {
      var li = document.createElement('li');
      li.textContent = (it.start ? it.start.replace('T', ' ') : (it.window || 'anytime')) + ' - ' + it.title;
      timeline.appendChild(li);
    });
}
function refresh() {
  fetch('/api/state').then(function (r) { return r.json(); }).then(function (s) {
    logEl.innerHTML = '';
    activeItem = null;
    s.messages.forEach(function (m) {
      addLine(m.role === 'agent' ? 'agent' : 'person', (m.role === 'agent' ? 'Agent: ' : 'You: ') + m.text);
      if (m.steps) {
        m.steps.forEach(function (st) { addLine('agent', '  - ' + st.title + ' (' + st.estimate_min + ' min)'); });
      }
      if (m.role === 'agent') {
        activeItem = CHAT_KINDS.indexOf(m.kind) >= 0 ? m.item_id : null;
      }
    });
    renderTimeline(s.items);
  }).catch(function () { addLine('agent', 'Could not load your plan just now.'); });
  fetch('/api/focus').then(function (r) { return r.json(); }).then(renderFocus).catch(function () {});
}
document.getElementById('chat-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var text = input.value.trim();
  if (!text) { return; }
  input.value = '';
  var req;
  if (activeItem) {
    req = post('/api/chat', {item_id: activeItem, text: text});
  } else if (text.indexOf('dump:') === 0) {
    req = post('/api/command', {text: text.slice(5), mode: 'dump'});
  } else {
    req = post('/api/command', {text: text, mode: 'command'});
  }
  req.then(function (r) {
    if (r.error) { addLine('agent', r.error); }
    refresh();
  }).catch(function () { addLine('agent', 'Connection hiccup, please try again.'); });
});
refresh();
</script>
</body></html>
"""


def read_index():
    try:
        with open(os.path.join(APP_DIR, "index.html"), "rb") as fh:
            return fh.read()
    except OSError:
        return BUILTIN_PAGE.encode("utf-8")


# ----------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "EvolvingCalendar/1.0"

    def log_message(self, fmt, *args):
        return

    def _send_bytes(self, code, content_type, data):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _send_json(self, code, obj):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            code = 500
            data = json.dumps({"error": "could not encode response"}).encode("utf-8")
        self._send_bytes(code, "application/json; charset=utf-8", data)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "body must be valid JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "body must be a JSON object")
        return obj

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_bytes(200, "text/html; charset=utf-8", read_index())
                return
            with LOCK:
                code, obj = route_get(path)
            self._send_json(code, obj)
        except ApiError as err:
            self._send_json(err.code, {"error": err.message})
        except Exception as err:
            self._send_json(500, {"error": "internal error: " + str(err)})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            body = self._read_body()
            with LOCK:
                code, obj = route_post(path, body)
                save_snapshot()
            self._send_json(code, obj)
        except ApiError as err:
            self._send_json(err.code, {"error": err.message})
        except Exception as err:
            self._send_json(500, {"error": "internal error: " + str(err)})


def main():
    global DATA_DIR
    parser = argparse.ArgumentParser(description="Evolving Calendar (Iris build) server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=os.path.join(APP_DIR, "data"))
    args = parser.parse_args()
    DATA_DIR = args.data
    os.makedirs(DATA_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.daemon_threads = True
    print(f"Evolving Calendar listening on port {args.port}, data in {DATA_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
