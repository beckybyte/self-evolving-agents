import argparse
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, date, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import v2
import v3

# === SECTION: constants ===

ISO = "%Y-%m-%dT%H:%M"

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot", "lazy", "urgent", "alert"]

KIND_REPLACEMENTS = [
    ("overdue", "still here"),
    ("fail", "not yet"),
    ("miss", "not yet"),
    ("late", "later on"),
    ("behind", "a bit back"),
    ("should have", "could"),
    ("forgot", "let slip"),
    ("lazy", "tired"),
    ("urgent", "worth a look"),
    ("alert", "note"),
]

VAGUE_VERBS = [
    "work on", "figure out", "deal with", "sort out", "sort through", "handle", "tackle",
    "get on top of", "catch up on", "clean out", "clean up", "organize", "organise", "plan",
    "prepare", "research", "finish", "start on", "apply for", "overhaul", "redo", "revamp",
    "get together", "get done", "get sorted", "put together", "set up", "look into",
    "think about", "go through", "wrap up", "follow up on", "reach out to", "check on",
    "review", "update", "improve", "fix up", "clear out", "declutter", "streamline",
    "get through", "work through", "map out", "sketch out", "flesh out", "build out",
    "write up", "clean", "tidy", "arrange", "coordinate", "manage", "develop", "create",
    "design", "build", "launch", "kick off", "get started on", "get going on",
]

BIG_NOUNS = [
    "project", "portfolio", "pitch deck", "deck", "thesis", "website", "garage", "closet",
    "inbox", "taxes", "application", "claim", "move", "renovation", "novel", "budget",
    "wedding", "conference", "presentation", "report", "business plan", "grant",
    "manuscript", "dissertation", "kitchen", "basement", "attic", "storage", "paperwork",
    "finances", "insurance", "visa", "immigration", "job search", "resume", "cv",
    "garden", "yard", "album", "book", "course", "class", "degree", "startup",
    "campaign", "newsletter", "blog", "podcast", "app", "product", "prototype",
]

YES_WORDS = [
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "please", "looks good",
    "sounds good", "good", "great", "perfect", "fine", "do it", "go ahead", "let's",
    "works", "that works", "why not", "of course", "absolutely", "love it", "nice",
    "deal", "cool", "awesome", "agreed", "go for it", "sounds right", "all good",
    "add them", "thanks", "thank you", "y",
]

NO_WORDS = [
    "no", "nope", "nah", "not now", "not right now", "no thanks", "later", "rather not",
    "don't", "dont", "not really", "not today",
]

CHANGE_WORDS = [
    "change", "different", "smaller", "shorter", "fewer", "more", "less", "instead",
    "rather", "swap", "replace", "split", "too big", "too long", "too much", "but",
]

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

WINDOWS = {
    "around noon": (12, 0, 13, 0),
    "noon": (12, 0, 13, 0),
    "lunch": (12, 0, 13, 0),
    "morning": (9, 0, 12, 0),
    "this morning": (9, 0, 12, 0),
    "afternoon": (14, 0, 17, 0),
    "this afternoon": (14, 0, 17, 0),
    "evening": (18, 0, 21, 0),
    "this evening": (18, 0, 21, 0),
    "tonight": (20, 0, 22, 0),
}

STEP_VERBS = [
    "List", "Pick", "Write", "Open", "Collect", "Choose", "Draft", "Email", "Call", "Book",
    "Clear", "Sort", "Find", "Gather", "Send", "Review", "Update", "Upload", "Read",
]

# === SECTION: state ===

STATE = {
    "now": "2026-10-19T09:00",
    "items": [],
    "messages": [],
    "convo": {},
    "undo": {},
    "sent": {},
    "streak_yes": 0,
    "streak_no": 0,
}

LOCK = threading.RLock()

DATA_DIR = None

_id_counter = [0]


def new_id(prefix="i"):
    _id_counter[0] += 1
    return "%s%d" % (prefix, _id_counter[0])


# === SECTION: time helpers ===

def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, ISO)
    except Exception:
        return None


def fmt_iso(dt):
    if dt is None:
        return None
    return dt.strftime(ISO)


def now_dt():
    return parse_iso(STATE["now"]) or datetime(2026, 10, 19, 9, 0)


def combine_dt(day_d, hour, minute=0):
    return datetime.combine(day_d, dtime(hour, minute))


# === SECTION: wording check ===

def clean_text(text):
    if not text:
        return text
    out = text
    low = out.lower()
    for bad, good in KIND_REPLACEMENTS:
        if bad in low:
            out = re.sub(re.escape(bad), good, out, flags=re.IGNORECASE)
            low = out.lower()
    return out


def has_banned(text):
    if not text:
        return False
    low = text.lower()
    for bad in BANNED:
        if bad in low:
            return True
    return False


# === SECTION: item helpers ===

def default_item():
    return {
        "id": None,
        "kind": "task",
        "title": "",
        "start": None,
        "end": None,
        "window": None,
        "status": "open",
        "why": None,
        "parent_id": None,
        "estimate_min": None,
        "postpone_count": 0,
        "source": "seed",
    }


def normalize_item(raw):
    it = default_item()
    if isinstance(raw, dict):
        for k in it.keys():
            if k in raw and raw[k] is not None:
                it[k] = raw[k]
        if raw.get("id"):
            it["id"] = raw["id"]
    if not it["id"]:
        it["id"] = new_id("s")
    if it["postpone_count"] is None:
        it["postpone_count"] = 0
    if it["status"] is None:
        it["status"] = "open"
    if it["source"] is None:
        it["source"] = "seed"
    return it


def find_item(item_id):
    for it in STATE["items"]:
        if it["id"] == item_id:
            return it
    return None


def open_items():
    return [it for it in STATE["items"] if it.get("status") == "open"]


def children_of(item_id):
    return [it for it in STATE["items"] if it.get("parent_id") == item_id]


def open_children_of(item_id):
    return [it for it in STATE["items"] if it.get("parent_id") == item_id and it.get("status") == "open"]


# === SECTION: messages ===

def add_agent(kind, text, item_id=None, undo_id=None, steps=None):
    text = clean_text(text)
    msg = {
        "id": new_id("m"),
        "role": "agent",
        "kind": kind,
        "text": text,
        "item_id": item_id,
        "at": STATE["now"],
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        msg["steps"] = steps or []
    STATE["messages"].append(msg)
    return msg


def add_person(text, item_id=None):
    msg = {
        "id": new_id("m"),
        "role": "person",
        "kind": "reply",
        "text": text,
        "item_id": item_id,
        "at": STATE["now"],
        "undo_id": None,
    }
    STATE["messages"].append(msg)
    return msg


# === SECTION: parsing ===

def strip_words(text, words):
    out = text
    for w in words:
        out = re.sub(r"\b" + re.escape(w) + r"\b", " ", out, flags=re.IGNORECASE)
    return out


def find_date(text, base_dt):
    low = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
        except Exception:
            pass
    m = re.search(r"\b(" + "|".join(MONTHS.keys()) + r")\.?\s+(\d{1,2})\b", low)
    if m:
        mon = MONTHS[m.group(1)]
        day = int(m.group(2))
        year = base_dt.year
        try:
            d = date(year, mon, day)
            if d < base_dt.date() - timedelta(days=180):
                d = date(year + 1, mon, day)
            return d, m.group(0)
        except Exception:
            pass
    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS.keys()) + r")\b", low)
    if m:
        day = int(m.group(1))
        mon = MONTHS[m.group(2)]
        year = base_dt.year
        try:
            d = date(year, mon, day)
            if d < base_dt.date() - timedelta(days=180):
                d = date(year + 1, mon, day)
            return d, m.group(0)
        except Exception:
            pass
    if re.search(r"\btoday\b", low):
        return base_dt.date(), "today"
    if re.search(r"\btomorrow\b", low):
        return base_dt.date() + timedelta(days=1), "tomorrow"
    for name, wd in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            today = base_dt.date()
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta), name
    return None, None


def find_time(text):
    low = text.lower()
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        ap = m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return h, mi, m.group(0)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1))
        ap = m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return h, 0, m.group(0)
    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        return h, 0, m.group(0)
    return None, None, None


def find_window(text):
    low = text.lower()
    best = None
    for w in WINDOWS.keys():
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            if best is None or len(w) > len(best):
                best = w
    return best


def is_appointment(text):
    low = text.lower()
    for w in ["with", "meet", "lunch", "dinner", "call with", "coffee", "drinks"]:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return True
    return False


def clean_title(text):
    t = text.strip()
    t = re.sub(r"[?!]+$", "", t).strip()
    t = re.sub(r"^(maybe|perhaps)\s+", "", t, flags=re.IGNORECASE).strip()
    if not t:
        return "New task"
    return t[0].upper() + t[1:]


def parse_text(text, base_dt=None):
    if base_dt is None:
        base_dt = now_dt()
    raw = text.strip()
    low = raw.lower()
    is_note = low.startswith("maybe") or raw.endswith("?")
    day_d, date_word = find_date(raw, base_dt)
    work = raw
    if date_word:
        work = re.sub(re.escape(date_word), " ", work, flags=re.IGNORECASE)
    win = find_window(work)
    hour, minute, time_word = find_time(work)
    if time_word:
        work = re.sub(re.escape(time_word), " ", work, flags=re.IGNORECASE)
    if win:
        work = re.sub(re.escape(win), " ", work, flags=re.IGNORECASE)
    title = clean_title(work)
    result = {
        "kind": "task",
        "title": title,
        "start": None,
        "end": None,
        "window": None,
        "estimate_min": None,
    }
    if hour is not None:
        if day_d is None:
            day_d = base_dt.date()
            cand = combine_dt(day_d, hour, minute)
            if cand <= base_dt:
                day_d = day_d + timedelta(days=1)
        start_dt = combine_dt(day_d, hour, minute)
        result["kind"] = "event"
        result["start"] = fmt_iso(start_dt)
        result["end"] = fmt_iso(start_dt + timedelta(minutes=60))
        return result
    if win:
        if day_d is None:
            day_d = base_dt.date()
        h1, m1, h2, m2 = WINDOWS[win]
        start_dt = combine_dt(day_d, h1, m1)
        end_dt = combine_dt(day_d, h2, m2)
        result["window"] = win
        result["start"] = fmt_iso(start_dt)
        result["end"] = fmt_iso(end_dt)
        result["kind"] = "event" if is_appointment(raw) else "task"
        return result
    if is_note:
        result["kind"] = "note"
    return result


def split_dump(text):
    parts = re.split(r"[,;\n]+", text)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


# === SECTION: complexity check ===

def is_big(text, estimate_min=None):
    low = " " + text.lower() + " "
    for v in VAGUE_VERBS:
        if " " + v + " " in low:
            return True
    for n in BIG_NOUNS:
        if re.search(r"\b" + re.escape(n) + r"\b", low):
            return True
    if estimate_min is not None and estimate_min > 120:
        return True
    return False


def complexity_question(item):
    return "This one sounds big. Want me to break '%s' into small steps?" % item["title"]


def maybe_ask_complexity(item):
    """Returns list of new messages. Applies APPETITE rules."""
    if not is_big(item["title"], item.get("estimate_min")):
        return []
    appetite = STATE.get("meta", {}).get("breakdown_appetite", 0.5)
    streak_no = STATE.get("streak_no", 0)
    streak_yes = STATE.get("streak_yes", 0)
    if streak_no >= 3 or appetite <= 0.1:
        return []
    if streak_yes >= 3 or appetite >= 0.9:
        deadline_d = now_dt().date() + timedelta(days=7)
        steps = build_steps(item["title"])
        msg = add_agent("breakdown", "Here's a gentle plan for '%s'. Does this look ok?" % item["title"],
                        item_id=item["id"], steps=steps)
        STATE["convo"][item["id"]] = "propose"
        STATE["convo"][item["id"] + ":deadline"] = fmt_iso(combine_dt(deadline_d, 23, 59))
        return [msg]
    msg = add_agent("question", complexity_question(item), item_id=item["id"])
    STATE["convo"][item["id"]] = "ask_split"
    return [msg]


# === SECTION: breakdown builder ===

def build_steps(title):
    low = title.lower()
    if "portfolio" in low:
        return [
            {"title": "List the projects you are proudest of", "estimate_min": 10},
            {"title": "Pick the five best projects", "estimate_min": 30},
            {"title": "Write a short note for each project", "estimate_min": 60},
            {"title": "Upload the projects to a simple website", "estimate_min": 60},
        ]
    if "pitch deck" in low or "deck" in low:
        return [
            {"title": "List the three points you want to make", "estimate_min": 10},
            {"title": "Draft the opening slide", "estimate_min": 30},
            {"title": "Write one slide per point", "estimate_min": 60},
            {"title": "Review the deck out loud", "estimate_min": 30},
        ]
    if "inbox" in low:
        return [
            {"title": "Open the inbox and list what needs a reply", "estimate_min": 10},
            {"title": "Clear the easy ones", "estimate_min": 30},
            {"title": "Draft replies to the rest", "estimate_min": 45},
        ]
    if "closet" in low or "garage" in low or "basement" in low or "attic" in low:
        return [
            {"title": "Open a box and list what is inside", "estimate_min": 10},
            {"title": "Pick what to keep", "estimate_min": 30},
            {"title": "Gather what to give away", "estimate_min": 45},
            {"title": "Clear the space", "estimate_min": 60},
        ]
    if "taxes" in low or "claim" in low or "application" in low or "insurance" in low:
        return [
            {"title": "List the papers you need", "estimate_min": 10},
            {"title": "Gather the papers", "estimate_min": 30},
            {"title": "Fill in the first section", "estimate_min": 45},
            {"title": "Review and send it", "estimate_min": 30},
        ]
    return [
        {"title": "Open a note and list what '%s' needs" % title, "estimate_min": 10},
        {"title": "Pick the first piece to start", "estimate_min": 20},
        {"title": "Draft the first piece", "estimate_min": 45},
        {"title": "Review it and send or save it", "estimate_min": 30},
    ]


def build_smaller_steps(title):
    steps = build_steps(title)
    out = []
    for s in steps[:5]:
        out.append({"title": s["title"], "estimate_min": min(s["estimate_min"], 30)})
    if len(out) < 2:
        out.append({"title": "Review it and send or save it", "estimate_min": 20})
    return out


# === SECTION: scheduler helpers ===

def usual_hour():
    done = [it for it in STATE["items"] if it.get("status") == "done" and it.get("start")]
    hours = []
    for it in done:
        dt = parse_iso(it["start"])
        if dt:
            hours.append(dt.hour)
    if not hours:
        return 19
    hours.sort()
    mid = hours[len(hours) // 2]
    return mid


def overlaps(start_dt, end_dt, ignore_id=None):
    for it in STATE["items"]:
        if it.get("status") != "open":
            continue
        if ignore_id and it["id"] == ignore_id:
            continue
        s = parse_iso(it.get("start"))
        e = parse_iso(it.get("end"))
        if s is None:
            continue
        if e is None:
            e = s + timedelta(minutes=60)
        if start_dt < e and s < end_dt:
            return True
    return False


def find_free_slot(day_d, length_min, after_dt=None, prefer_hour=None):
    if prefer_hour is None:
        prefer_hour = usual_hour()
    start_h = prefer_hour
    start_m = 0
    while start_h < 24:
        cand = combine_dt(day_d, start_h, start_m)
        cand_end = cand + timedelta(minutes=length_min)
        if after_dt is not None and cand <= after_dt:
            start_m += 15
            if start_m >= 60:
                start_m = 0
                start_h += 1
            continue
        if not overlaps(cand, cand_end):
            return cand
        start_m += 15
        if start_m >= 60:
            start_m = 0
            start_h += 1
    start_h = 8
    start_m = 0
    while start_h < 24:
        cand = combine_dt(day_d, start_h, start_m)
        cand_end = cand + timedelta(minutes=length_min)
        if after_dt is not None and cand <= after_dt:
            start_m += 15
            if start_m >= 60:
                start_m = 0
                start_h += 1
            continue
        if not overlaps(cand, cand_end):
            return cand
        start_m += 15
        if start_m >= 60:
            start_m = 0
            start_h += 1
    return combine_dt(day_d, 23, 0)


def schedule_children(goal, steps, deadline_dt):
    created = []
    base_dt = now_dt()
    day_d = base_dt.date()
    hour = usual_hour()
    slot = find_free_slot(day_d, steps[0]["estimate_min"], after_dt=base_dt, prefer_hour=hour)
    if slot is None or slot <= base_dt:
        day_d = day_d + timedelta(days=1)
        slot = find_free_slot(day_d, steps[0]["estimate_min"], prefer_hour=hour)
    for i, s in enumerate(steps):
        est = s["estimate_min"]
        if i > 0:
            day_d = day_d + timedelta(days=1)
            slot = find_free_slot(day_d, est, prefer_hour=hour)
        if deadline_dt is not None and slot >= deadline_dt:
            last_day = deadline_dt.date() - timedelta(days=1)
            if last_day < base_dt.date():
                last_day = base_dt.date()
            slot = find_free_slot(last_day, est, after_dt=base_dt, prefer_hour=8)
            if slot is None:
                slot = combine_dt(last_day, 8, 0)
        child = default_item()
        child["id"] = new_id("c")
        child["kind"] = "task"
        child["title"] = s["title"]
        child["start"] = fmt_iso(slot)
        child["end"] = fmt_iso(slot + timedelta(minutes=est))
        child["status"] = "open"
        child["parent_id"] = goal["id"]
        child["estimate_min"] = est
        child["source"] = "agent"
        STATE["items"].append(child)
        created.append(child)
    return created


# === SECTION: store / reset ===

def do_reset(body):
    now = body.get("now") or "2026-10-19T09:00"
    STATE["now"] = now
    STATE["items"] = []
    STATE["messages"] = []
    STATE["convo"] = {}
    STATE["undo"] = {}
    STATE["sent"] = {}
    STATE["streak_yes"] = 0
    STATE["streak_no"] = 0
    seeds = body.get("items") or []
    for raw in seeds:
        it = normalize_item(raw)
        STATE["items"].append(it)
    v2.init_v2(STATE)
    STATE["meta"]["breakdown_appetite"] = 0.5
    STATE["meta"]["threshold"] = 0.7
    add_agent("info", "Hi, I'm here whenever you want to plan a little.")
    return state_response()


def state_response():
    return {
        "now": STATE["now"],
        "items": STATE["items"],
        "messages": STATE["messages"],
        "view": v2.view_state(STATE),
        "meta": v2.meta_state(STATE),
    }


# === SECTION: command handling ===

def handle_command(text, mode):
    new_messages = []
    created_ids = []
    if mode == "dump":
        fragments = split_dump(text)
        titles = []
        first_big = None
        for frag in fragments:
            parsed = parse_text(frag)
            item = default_item()
            item["id"] = new_id("d")
            item["kind"] = parsed["kind"]
            item["title"] = parsed["title"]
            item["start"] = parsed["start"]
            item["end"] = parsed["end"]
            item["window"] = parsed["window"]
            item["estimate_min"] = parsed["estimate_min"]
            item["source"] = "dump"
            STATE["items"].append(item)
            created_ids.append(item["id"])
            titles.append(item["title"])
            v2.add_log(STATE, "create", [item["id"]], {"title": item["title"]}, "dump")
            if first_big is None and is_big(item["title"], item.get("estimate_min")):
                first_big = item
        listing = ", ".join(titles)
        new_messages.append(add_agent("info", "Added: %s." % listing))
        if first_big is not None:
            new_messages.extend(maybe_ask_complexity(first_big))
        return {"messages": new_messages, "created": created_ids}

    # mode == "command"
    v2_result = v2.handle_command(STATE, text)
    if v2_result is not None:
        return v2_result

    parsed = parse_text(text)
    item = default_item()
    item["id"] = new_id("t")
    item["kind"] = parsed["kind"]
    item["title"] = parsed["title"]
    item["start"] = parsed["start"]
    item["end"] = parsed["end"]
    item["window"] = parsed["window"]
    item["estimate_min"] = parsed["estimate_min"]
    item["source"] = "typed"
    STATE["items"].append(item)
    created_ids.append(item["id"])
    v2.add_log(STATE, "create", [item["id"]], {"title": item["title"]}, "typed")
    new_messages.append(add_agent("info", "Added '%s'." % item["title"]))
    new_messages.extend(maybe_ask_complexity(item))
    return {"messages": new_messages, "created": created_ids}


# === SECTION: chat state machine ===

def has_yes(text):
    low = " " + text.lower() + " "
    for w in YES_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
            return True
    return False


def has_no(text):
    low = " " + text.lower() + " "
    for w in NO_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
            return True
    return False


def has_change(text):
    low = " " + text.lower() + " "
    for w in CHANGE_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low):
            return True
    return False


def handle_chat(item_id, text):
    item = find_item(item_id)
    if item is None:
        raise KeyError(item_id)
    new_messages = []
    add_person(text, item_id)
    state = STATE["convo"].get(item_id)
    was_checkin = state in ("checkin", "confirm_drop")

    if state == "ask_split":
        if has_no(text):
            STATE["streak_no"] = STATE.get("streak_no", 0) + 1
            STATE["streak_yes"] = 0
            appetite = STATE["meta"].get("breakdown_appetite", 0.5)
            STATE["meta"]["breakdown_appetite"] = round(max(0.0, appetite - 0.15), 2)
            STATE["convo"][item_id] = None
            new_messages.append(add_agent("info", "Okay, I'll keep it as one task.", item_id=item_id))
        elif has_yes(text):
            STATE["streak_yes"] = STATE.get("streak_yes", 0) + 1
            STATE["streak_no"] = 0
            appetite = STATE["meta"].get("breakdown_appetite", 0.5)
            STATE["meta"]["breakdown_appetite"] = round(min(1.0, appetite + 0.15), 2)
            STATE["convo"][item_id] = "ask_why"
            new_messages.append(add_agent("question", "Why does this matter to you?", item_id=item_id))
        else:
            new_messages.append(add_agent("question",
                                          "No pressure. Shall I break it into small steps, yes or not now?",
                                          item_id=item_id))
        return {"messages": new_messages}

    if state == "ask_why":
        item["why"] = text
        STATE["convo"][item_id] = "ask_done"
        new_messages.append(add_agent("question", "What does done look like for you?", item_id=item_id))
        return {"messages": new_messages}

    if state == "ask_done":
        STATE["convo"][item_id + ":done"] = text
        STATE["convo"][item_id] = "ask_deadline"
        new_messages.append(add_agent("question",
                                      "Is there a deadline, a day you'd like it finished by?",
                                      item_id=item_id))
        return {"messages": new_messages}

    if state == "ask_deadline":
        base_dt = now_dt()
        day_d, _ = find_date(text, base_dt)
        if day_d is None:
            day_d = base_dt.date() + timedelta(days=7)
        deadline_dt = combine_dt(day_d, 23, 59)
        STATE["convo"][item_id + ":deadline"] = fmt_iso(deadline_dt)
        steps = build_steps(item["title"])
        msg = add_agent("breakdown", "Here's a gentle plan for '%s'. Does this look ok?" % item["title"],
                        item_id=item_id, steps=steps)
        STATE["convo"][item_id] = "propose"
        new_messages.append(msg)
        return {"messages": new_messages}

    if state == "propose":
        if has_change(text) and not (has_yes(text) and not has_change(text)):
            steps = build_smaller_steps(item["title"])
            msg = add_agent("breakdown", "Here's a gentler version for '%s'. Does this look ok?" % item["title"],
                            item_id=item_id, steps=steps)
            new_messages.append(msg)
            return {"messages": new_messages}
        if has_yes(text):
            steps = build_steps(item["title"])
            deadline_iso = STATE["convo"].get(item_id + ":deadline")
            deadline_dt = parse_iso(deadline_iso) if deadline_iso else None
            created = schedule_children(item, steps, deadline_dt)
            for c in created:
                v2.add_log(STATE, "create", [c["id"]], {"title": c["title"]}, "agent")
            STATE["convo"][item_id] = None
            first = steps[0]["title"] if steps else "the first step"
            new_messages.append(add_agent("info", "Done, the first tiny step is '%s'." % first, item_id=item_id))
            return {"messages": new_messages}
        if has_no(text):
            STATE["convo"][item_id] = None
            new_messages.append(add_agent("info", "Okay, I'll keep it as one task.", item_id=item_id))
            return {"messages": new_messages}
        new_messages.append(add_agent("question", "Shall I add these steps to your calendar?", item_id=item_id))
        return {"messages": new_messages}

    if state == "checkin":
        low = text.lower()
        if "big" in low or "smaller" in low or "break" in low or "too much" in low:
            steps = build_steps(item["title"])
            msg = add_agent("breakdown", "Here's a gentle plan for '%s'. Does this look ok?" % item["title"],
                            item_id=item_id, steps=steps)
            STATE["convo"][item_id] = "propose"
            new_messages.append(msg)
            return {"messages": new_messages}
        if "time" in low or "busy" in low or "wrong" in low:
            base_dt = now_dt()
            length = 60
            s = parse_iso(item.get("start"))
            e = parse_iso(item.get("end"))
            if s and e:
                length = int((e - s).total_seconds() // 60)
            day_d = base_dt.date() + timedelta(days=1)
            slot = find_free_slot(day_d, length, after_dt=base_dt)
            undo_id = new_id("u")
            STATE["undo"][undo_id] = {
                "item_id": item["id"],
                "start": item.get("start"),
                "end": item.get("end"),
                "window": item.get("window"),
                "postpone_count": item.get("postpone_count", 0),
            }
            item["start"] = fmt_iso(slot)
            item["end"] = fmt_iso(slot + timedelta(minutes=length))
            STATE["convo"][item_id] = None
            v2.add_log(STATE, "update", [item["id"]], {"start": item["start"]}, "person")
            new_messages.append(add_agent("info",
                                          "Moved '%s' to %s." % (item["title"], slot.strftime("%a %b %d at %H:%M")),
                                          item_id=item_id, undo_id=undo_id))
            return {"messages": new_messages}
        if "not needed" in low or "drop" in low or "remove" in low or "don't need" in low or "dont need" in low:
            STATE["convo"][item_id] = "confirm_drop"
            new_messages.append(add_agent("question", "Shall I remove '%s' for you?" % item["title"],
                                          item_id=item_id))
            return {"messages": new_messages}
        new_messages.append(add_agent("info", "Okay, I'll keep '%s'." % item["title"], item_id=item_id))
        STATE["convo"][item_id] = None
        return {"messages": new_messages}

    if state == "confirm_drop":
        if has_yes(text):
            STATE["items"] = [it for it in STATE["items"] if it["id"] != item_id]
            STATE["convo"][item_id] = None
            v2.add_log(STATE, "delete", [item_id], {}, "person")
            new_messages.append(add_agent("info", "Removed '%s'." % item["title"]))
            return {"messages": new_messages}
        STATE["convo"][item_id] = None
        new_messages.append(add_agent("info", "Okay, I'll keep '%s'." % item["title"], item_id=item_id))
        return {"messages": new_messages}

    # default: no state
    new_messages.append(add_agent("info", "Okay, I'll keep '%s'." % item["title"], item_id=item_id))
    return {"messages": new_messages}


# === SECTION: clock + scheduler ===

def handle_clock(new_iso):
    new_dt = parse_iso(new_iso)
    if new_dt is None:
        return {"messages": []}
    cur = now_dt()
    if new_dt <= cur:
        return {"messages": []}
    new_messages = []
    t = cur + timedelta(minutes=1)
    checkin_sent = False
    while t <= new_dt:
        for it in list(STATE["items"]):
            if it.get("status") != "open":
                continue
            start = parse_iso(it.get("start"))
            end = parse_iso(it.get("end"))
            if start is not None and start == t:
                key = (it["id"], "start", it["start"])
                if key not in STATE["sent"]:
                    STATE["sent"][key] = True
                    msg = add_agent("reminder", "It's time for '%s' (%s)." % (it["title"], start.strftime("%H:%M")),
                                    item_id=it["id"])
                    new_messages.append(msg)
                    v2.add_log(STATE, "reminder", [it["id"]], {"start": it["start"]}, "clock")
            if end is not None and end - timedelta(minutes=10) == t:
                key = (it["id"], "end", it["end"])
                if key not in STATE["sent"]:
                    STATE["sent"][key] = True
                    msg = add_agent("reminder", "'%s' wraps up in 10 minutes." % it["title"], item_id=it["id"])
                    new_messages.append(msg)
                    v2.add_log(STATE, "reminder", [it["id"]], {"end": it["end"]}, "clock")
            if it.get("kind") != "task":
                continue
            if children_of(it["id"]):
                continue
            if start is None:
                continue
            if end is None:
                end = start + timedelta(minutes=60)
            if t >= end + timedelta(minutes=31):
                if it.get("postpone_count", 0) >= 2:
                    key = (it["id"], "checkin", it["end"])
                    if key in STATE["sent"]:
                        continue
                    if checkin_sent:
                        continue
                    STATE["sent"][key] = True
                    checkin_sent = True
                    msg = add_agent("checkin",
                                    "'%s' has moved a few times, and that's okay. Shall I break it into smaller steps, find a better time, or let it go?" % it["title"],
                                    item_id=it["id"])
                    new_messages.append(msg)
                    STATE["convo"][it["id"]] = "checkin"
                    v2.add_log(STATE, "checkin_answer", [it["id"]], {}, "clock")
                else:
                    length = int((end - start).total_seconds() // 60)
                    if length <= 0:
                        length = 60
                    day_d = end.date() + timedelta(days=1)
                    if day_d < t.date():
                        day_d = t.date()
                    slot = find_free_slot(day_d, length, after_dt=t)
                    if slot is None:
                        slot = combine_dt(day_d, 23, 0)
                    undo_id = new_id("u")
                    STATE["undo"][undo_id] = {
                        "item_id": it["id"],
                        "start": it.get("start"),
                        "end": it.get("end"),
                        "window": it.get("window"),
                        "postpone_count": it.get("postpone_count", 0),
                    }
                    it["start"] = fmt_iso(slot)
                    it["end"] = fmt_iso(slot + timedelta(minutes=length))
                    it["postpone_count"] = it.get("postpone_count", 0) + 1
                    msg = add_agent("postponed",
                                    "No worries, I moved '%s' to %s. Undo if you'd rather keep it." % (
                                        it["title"], slot.strftime("%a %b %d at %H:%M")),
                                    item_id=it["id"], undo_id=undo_id)
                    new_messages.append(msg)
                    v2.add_log(STATE, "postponed", [it["id"]], {"start": it["start"]}, "clock")
        t = t + timedelta(minutes=1)
    STATE["now"] = new_iso
    v2.tick(STATE, True)
    return {"messages": new_messages}


# === SECTION: focus ===

def handle_focus():
    goals = [it for it in STATE["items"] if it.get("status") == "open" and it.get("why")]
    best = None
    best_parent = None
    for g in goals:
        for c in open_children_of(g["id"]):
            s = parse_iso(c.get("start"))
            key = (s is None, s or datetime.max, STATE["items"].index(c))
            if best is None or key < best[0]:
                best = (key, c, g)
    if best is not None:
        c = best[1]
        g = best[2]
        return {"item": c, "parent": g, "why": g.get("why"), "action": "Start 5 minutes"}
    candidates = []
    for it in STATE["items"]:
        if it.get("status") != "open":
            continue
        if children_of(it["id"]):
            continue
        s = parse_iso(it.get("start"))
        key = (s is None, s or datetime.max, STATE["items"].index(it))
        candidates.append((key, it))
    if not candidates:
        return {"item": None, "parent": None, "why": None, "action": "Start 5 minutes"}
    candidates.sort(key=lambda x: x[0])
    it = candidates[0][1]
    parent = None
    why = it.get("why")
    if it.get("parent_id"):
        parent = find_item(it["parent_id"])
        if parent and parent.get("why"):
            why = parent["why"]
    return {"item": it, "parent": parent, "why": why, "action": "Start 5 minutes"}


# === SECTION: done / undo ===

def handle_done(item_id):
    item = find_item(item_id)
    if item is None:
        raise KeyError(item_id)
    item["status"] = "done"
    v2.add_log(STATE, "done", [item_id], {}, "person")
    return {"item": item}


def handle_undo(undo_id):
    rec = STATE["undo"].get(undo_id)
    if rec is None:
        raise KeyError(undo_id)
    item = find_item(rec["item_id"])
    if item is not None:
        item["start"] = rec.get("start")
        item["end"] = rec.get("end")
        item["window"] = rec.get("window")
        item["postpone_count"] = rec.get("postpone_count", 0)
        v2.add_log(STATE, "update", [item["id"]], {}, "person")
    return {"ok": True}


# === SECTION: HTTP handler ===

INDEX_FALLBACK = """<!doctype html><html><head><meta charset="utf-8"><title>Calendar</title></head>
<body><h1>Calendar</h1>
<div id="chat-log"></div>
<input id="chat-input">
<div id="focus"></div>
<div id="timeline"></div>
<div id="learned"></div>
<div id="activity"></div>
<div id="proposal"></div>
<div id="view-switcher"></div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, obj):
        try:
            body = json.dumps(obj).encode("utf-8")
        except Exception:
            body = b'{"error": "serialization"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        with LOCK:
            try:
                path = self.path.split("?")[0]
                if path == "/health":
                    return self._send(200, {"ok": True})
                if path == "/":
                    return self._serve_index()
                if path == "/api/state":
                    return self._send(200, state_response())
                if path == "/api/focus":
                    return self._send(200, handle_focus())
                if path == "/api/log":
                    return self._send(200, v2.get_log(STATE))
                if path == "/api/beliefs":
                    return self._send(200, v2.get_beliefs(STATE))
                if path == "/api/proposal":
                    return self._send(200, v2.get_proposal(STATE))
                if path == "/api/prompts":
                    return self._send(200, v2.get_prompts(STATE))
                if path == "/api/activity":
                    return self._send(200, v2.get_activity(STATE))
                if path == "/api/catalog":
                    return self._send(200, v3.get_catalog(STATE))
                return self._send(404, {"error": "not found"})
            except KeyError as e:
                return self._send(404, {"error": "not found: %s" % e})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})

    def do_DELETE(self):
        with LOCK:
            try:
                path = self.path.split("?")[0]
                m = re.match(r"^/api/beliefs/(.+)$", path)
                if m:
                    key = m.group(1)
                    return self._send(200, v2.delete_belief(STATE, key))
                return self._send(404, {"error": "not found"})
            except KeyError as e:
                return self._send(404, {"error": "not found: %s" % e})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})

    def do_POST(self):
        with LOCK:
            try:
                path = self.path.split("?")[0]
                body = self._read_body()
                if path == "/api/reset":
                    return self._send(200, do_reset(body))
                if path == "/api/command":
                    text = body.get("text") or ""
                    mode = body.get("mode") or "command"
                    result = handle_command(text, mode)
                    v2.tick(STATE, False)
                    return self._send(200, result)
                if path == "/api/chat":
                    item_id = body.get("item_id")
                    text = body.get("text") or ""
                    was_checkin = STATE["convo"].get(item_id) in ("checkin", "confirm_drop")
                    result = handle_chat(item_id, text)
                    kind = "checkin_answer" if was_checkin else "chat"
                    v2.add_log(STATE, kind, [item_id], {"text": text}, "person")
                    v2.tick(STATE, False)
                    return self._send(200, result)
                if path == "/api/clock":
                    result = handle_clock(body.get("now"))
                    return self._send(200, result)
                if path == "/api/undo":
                    return self._send(200, handle_undo(body.get("undo_id")))
                m = re.match(r"^/api/items/([^/]+)/done$", path)
                if m:
                    return self._send(200, handle_done(m.group(1)))
                if path == "/api/import":
                    return self._send(200, v2.import_file(STATE, body.get("filename"), body.get("content")))
                m = re.match(r"^/api/plan/([^/]+)/apply$", path)
                if m:
                    return self._send(200, v2.apply_plan(STATE, m.group(1)))
                m = re.match(r"^/api/plan/([^/]+)/undo$", path)
                if m:
                    return self._send(200, v2.undo_plan(STATE, m.group(1)))
                if path == "/api/learn":
                    return self._send(200, v2.learn(STATE))
                m = re.match(r"^/api/proposals/([^/]+)/([^/]+)$", path)
                if m:
                    return self._send(200, v2.proposal_action(STATE, m.group(1), m.group(2)))
                if path == "/api/view":
                    return self._send(200, v2.set_view(STATE, body.get("type")))
                m = re.match(r"^/api/prompts/([^/]+)/rollback$", path)
                if m:
                    return self._send(200, v2.rollback(STATE, m.group(1)))
                if path == "/api/critic":
                    return self._send(200, v3.critic(STATE, body.get("spec")))
                if path == "/api/design":
                    r = v3.design(STATE, body.get("belief_key"))
                    r["proposal"] = v2.learn(STATE)["proposal"]
                    return self._send(200, r)
                return self._send(404, {"error": "not found"})
            except KeyError as e:
                return self._send(404, {"error": "not found: %s" % e})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})

    def _serve_index(self):
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "index.html")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            content = INDEX_FALLBACK
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass


# === SECTION: selftest ===

def _st_reset(now="2026-10-19T09:00", items=None):
    return do_reset({"now": now, "items": items or []})


def _st_cmd(text, mode="command"):
    return handle_command(text, mode)


def _st_chat(item_id, text):
    return handle_chat(item_id, text)


def _st_clock(now):
    return handle_clock(now)


def _st_focus():
    return handle_focus()


def _st_done(item_id):
    return handle_done(item_id)


def _st_undo(undo_id):
    return handle_undo(undo_id)


def _st_import(filename, content):
    return v2.import_file(STATE, filename, content)


def _st_apply(plan_id):
    return v2.apply_plan(STATE, plan_id)


def _st_undo_plan(plan_id):
    return v2.undo_plan(STATE, plan_id)


def _st_learn():
    return v2.learn(STATE)


def _st_view(t):
    return v2.set_view(STATE, t)


def _st_proposal_action(pid, action):
    return v2.proposal_action(STATE, pid, action)


def _st_prompts():
    return v2.get_prompts(STATE)


def _st_rollback(agent):
    return v2.rollback(STATE, agent)


def _st_activity():
    return v2.get_activity(STATE)


def _st_log():
    return v2.get_log(STATE)


def _st_catalog():
    return v3.get_catalog(STATE)


def _st_critic(spec):
    return v3.critic(STATE, spec)


def _st_design(belief_key):
    r = v3.design(STATE, belief_key)
    r["proposal"] = v2.learn(STATE)["proposal"]
    return r


def _st_state():
    return state_response()


def _st_proposal():
    return v2.get_proposal(STATE)


def _st_beliefs():
    return v2.get_beliefs(STATE)


def _st_delete_belief(key):
    return v2.delete_belief(STATE, key)


def _st_meta():
    return v2.meta_state(STATE)


def _st_view_state():
    return v2.view_state(STATE)


def _st_ask_question_count(resp):
    n = 0
    for m in resp.get("messages", []):
        if m.get("role") != "agent":
            continue
        if m.get("kind") in ("question", "checkin", "breakdown") or "?" in (m.get("text") or ""):
            n += 1
    return n


def _st_check_no_bad(resp):
    for m in resp.get("messages", []):
        if m.get("role") != "agent":
            continue
        t = m.get("text") or ""
        assert t.count("?") <= 1, "app.py: agent text has more than one '?': %r" % t
        assert not has_banned(t), "app.py: agent text has a banned word: %r" % t
    assert _st_ask_question_count(resp) <= 1, "app.py: more than one asking agent message in a response"


def _st_seed_iris():
    return [
        {"id": "h0", "kind": "task", "title": "Old done 0", "start": "2026-10-13T20:15",
         "end": "2026-10-13T20:45", "status": "done", "source": "seed"},
        {"id": "h1", "kind": "task", "title": "Old done 1", "start": "2026-10-13T21:00",
         "end": "2026-10-13T21:40", "status": "done", "source": "seed"},
        {"id": "t1", "kind": "task", "title": "Write cover letter", "start": "2026-10-19T14:00",
         "end": "2026-10-19T15:00", "status": "open", "source": "seed"},
        {"id": "yo", "kind": "event", "title": "Yoga", "start": "2026-10-24T10:00",
         "end": "2026-10-24T11:00", "status": "open", "source": "seed"},
        {"id": "den", "kind": "event", "title": "Dentist", "start": "2026-10-22T15:00",
         "end": "2026-10-22T16:00", "status": "open", "source": "seed"},
    ]


def selftest():
    # ---- walk ----
    _st_reset("2026-10-19T09:00", _st_seed_iris())
    _st_cmd("dentist thursday 3pm")
    _st_cmd("lunch with Ana around noon tomorrow")
    _st_cmd("call mom, portfolio, maybe yoga?", "dump")
    r = _st_cmd("get my portfolio together")
    goal_id = None
    for m in r.get("messages", []):
        if m.get("kind") == "question" and m.get("item_id"):
            goal_id = m["item_id"]
    if goal_id is None:
        for it in STATE["items"]:
            if "portfolio" in it["title"].lower() and it.get("source") == "typed":
                goal_id = it["id"]
    if goal_id:
        _st_chat(goal_id, "yes, please")
        _st_chat(goal_id, "I want to feel proud")
        _st_chat(goal_id, "Five projects online")
        _st_chat(goal_id, "By Friday, October 23.")
        _st_chat(goal_id, "looks good")
    _st_focus()
    for it in STATE["items"]:
        if it.get("parent_id") == goal_id and it.get("status") == "open":
            _st_done(it["id"])
            break
    _st_cmd("what's on thursday?")
    _st_cmd("move my dentist to next week")
    r = _st_cmd("cancel yoga")
    if isinstance(r, dict) and r.get("plan"):
        _st_apply(r["plan"]["id"])
    r = _st_cmd("empty out my weekend for a business trip")
    if isinstance(r, dict) and r.get("plan"):
        _st_apply(r["plan"]["id"])
        _st_undo_plan(r["plan"]["id"])
    ics = ("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:a1\nDTSTART;TZID=Europe/Lisbon:20261025T100000\n"
           "DTEND;TZID=Europe/Lisbon:20261025T110000\nSUMMARY:Brunch\nEND:VEVENT\nBEGIN:VEVENT\nUID:a2\n"
           "DTSTART;VALUE=DATE:20261026\nSUMMARY:Day off\nEND:VEVENT\nEND:VCALENDAR\n")
    r = _st_import("cal.ics", ics)
    if isinstance(r, dict) and r.get("plan"):
        _st_apply(r["plan"]["id"])
    r = _st_import("note.txt", "I'll be away this weekend, please clear it. Also lunch with Ana on Wednesday around noon.")
    if isinstance(r, dict) and r.get("plan"):
        _st_apply(r["plan"]["id"])
    _st_learn()
    _st_view("workweek")
    p = _st_proposal().get("proposal")
    if p:
        _st_proposal_action(p["id"], "accept")
        _st_proposal_action(p["id"], "undo")
    _st_prompts()
    _st_rollback("designer")
    _st_activity()
    _st_log()
    _st_clock("2026-10-19T16:00")
    for m in STATE["messages"]:
        if m.get("kind") == "postponed" and m.get("undo_id"):
            _st_undo(m["undo_id"])
            break
    _st_clock("2026-10-23T23:00")
    if goal_id:
        _st_chat(goal_id, "honestly it feels too big")
    _st_state()
    _st_reset("2026-10-19T09:00", [])

    # ---- v3 walk ----
    _st_catalog()
    _st_critic({"root": "r", "elements": {"r": {"type": "SpiralClock", "props": {}, "children": []}}})
    _st_design(None)
    _st_design("evening_work")

    # appetite: three no's
    _st_reset("2026-10-19T09:00", [])
    for n in range(3):
        r = _st_cmd("work on task %d" % n)
        for m in r.get("messages", []):
            if m.get("kind") == "question" and m.get("item_id"):
                _st_chat(m["item_id"], "no")
    _st_cmd("work on my novel")

    # appetite: three yes's
    _st_reset("2026-10-19T09:00", [])
    for n in range(3):
        r = _st_cmd("work on task %d" % n)
        for m in r.get("messages", []):
            if m.get("kind") == "question" and m.get("item_id"):
                _st_chat(m["item_id"], "yes")
                _st_chat(m["item_id"], "because it matters")
                _st_chat(m["item_id"], "done")
                _st_chat(m["item_id"], "friday")
                _st_chat(m["item_id"], "no")
    _st_cmd("work on my novel")

    # three proposal dismisses
    for n in range(3):
        p = _st_proposal().get("proposal")
        if p:
            _st_proposal_action(p["id"], "dismiss")
        else:
            _st_learn()

    # ---- paraphrase walk ----
    _st_reset("2026-10-20T08:00", [
        {"id": "bike", "kind": "task", "title": "Bike repair", "start": "2026-10-24T10:00",
         "end": "2026-10-24T11:00", "status": "open", "source": "seed"},
        {"id": "pic", "kind": "task", "title": "Picnic", "start": "2026-10-24T13:00",
         "end": "2026-10-24T15:00", "status": "open", "source": "seed"},
        {"id": "choir", "kind": "task", "title": "Choir", "start": "2026-10-25T10:00",
         "end": "2026-10-25T11:30", "status": "open", "source": "seed"},
        {"id": "demo", "kind": "task", "title": "Demo", "start": "2026-10-23T15:00",
         "end": "2026-10-23T16:00", "status": "open", "source": "seed"},
    ])
    r = _st_cmd("free up my saturday, I have a family reunion")
    if isinstance(r, dict) and r.get("plan"):
        _st_apply(r["plan"]["id"])
        _st_undo_plan(r["plan"]["id"])
    _st_reset("2026-10-20T08:00", [
        {"id": "choir", "kind": "task", "title": "Choir", "start": "2026-10-25T10:00",
         "end": "2026-10-25T11:30", "status": "open", "source": "seed"},
    ])
    r = _st_cmd("nothing on sunday please, I'm travelling to Lisbon")
    if isinstance(r, dict) and r.get("plan"):
        _st_apply(r["plan"]["id"])
    _st_reset("2026-10-20T08:00", [
        {"id": "st", "kind": "task", "title": "Standup", "start": "2026-10-23T09:00",
         "end": "2026-10-23T09:30", "status": "open", "source": "seed"},
        {"id": "dr", "kind": "task", "title": "Drinks", "start": "2026-10-23T18:30",
         "end": "2026-10-23T20:00", "status": "open", "source": "seed"},
        {"id": "la", "kind": "task", "title": "Laundry", "start": "2026-10-23T21:00",
         "end": "2026-10-23T22:00", "status": "open", "source": "seed"},
    ])
    r = _st_cmd("wipe friday evening, I got concert tickets")
    if isinstance(r, dict) and r.get("plan"):
        _st_apply(r["plan"]["id"])
    _st_reset("2026-10-20T08:00", [
        {"id": "eye", "kind": "event", "title": "Eye exam", "start": "2026-10-23T14:00",
         "end": "2026-10-23T14:45", "status": "open", "source": "seed"},
        {"id": "pia", "kind": "event", "title": "Piano", "start": "2026-10-23T17:00",
         "end": "2026-10-23T18:00", "status": "open", "source": "seed"},
    ])
    _st_cmd("anything on the 23rd?")
    _st_cmd("push the dentist back a week")
    _st_cmd("drop yoga")
    _st_cmd("handle the insurance claim")
    _st_cmd("get on top of my inbox")
    r = _st_cmd("work on the grant application")
    gid = None
    for m in r.get("messages", []):
        if m.get("kind") == "question" and m.get("item_id"):
            gid = m["item_id"]
    if gid:
        _st_chat(gid, "sure, go ahead")
        _st_chat(gid, "It matters")
        _st_chat(gid, "It is submitted")
        _st_chat(gid, "by November 6")
        _st_chat(gid, "that works for me")

    # ---- assertions ----
    _st_reset("2026-10-19T09:00", [])
    r = _st_cmd("dentist thursday 3pm")
    _st_check_no_bad(r)
    assert len(r["created"]) == 1, "app.py: dentist command should create one item"
    it = find_item(r["created"][0])
    assert it["kind"] == "event", "app.py: dentist should be an event"
    assert it["start"] == "2026-10-22T15:00", "app.py: dentist start wrong: %r" % it["start"]

    _st_reset("2026-10-19T09:00", [])
    r = _st_cmd("call mom, portfolio, maybe yoga?, dentist thursday 3pm", "dump")
    _st_check_no_bad(r)
    assert len(r["created"]) == 4, "app.py: dump should create 4 items, got %d" % len(r["created"])
    q = [m for m in r["messages"] if m.get("kind") == "question"]
    assert len(q) <= 1, "app.py: dump should ask at most one question, got %d" % len(q)

    for text in ["handle the insurance claim", "get on top of my inbox", "sort out my closet"]:
        _st_reset("2026-10-19T09:00", [])
        r = _st_cmd(text)
        _st_check_no_bad(r)
        q = [m for m in r["messages"] if m.get("kind") == "question"]
        assert len(q) == 1, "app.py: %r should ask exactly one question, got %d" % (text, len(q))
        assert q[0].get("item_id") == r["created"][0], "app.py: question should be on the new item"
    _st_reset("2026-10-19T09:00", [])
    r = _st_cmd("call mom")
    _st_check_no_bad(r)
    q = [m for m in r["messages"] if m.get("kind") == "question"]
    assert len(q) == 0, "app.py: 'call mom' should not ask a question"

    _st_reset("2026-10-19T09:00", [])
    r = _st_cmd("work on the grant application")
    _st_check_no_bad(r)
    gid = None
    for m in r["messages"]:
        if m.get("kind") == "question" and m.get("item_id"):
            gid = m["item_id"]
    assert gid is not None, "app.py: grant application should ask a question"
    _st_chat(gid, "sure, go ahead")
    _st_chat(gid, "I want it done")
    _st_chat(gid, "It is submitted")
    _st_chat(gid, "by November 6")
    r = _st_chat(gid, "that works for me")
    _st_check_no_bad(r)
    kids = [it for it in STATE["items"] if it.get("parent_id") == gid and it.get("status") == "open"]
    assert len(kids) >= 2, "app.py: accept should create at least 2 open children, got %d" % len(kids)
    goal = find_item(gid)
    assert goal["why"] == "I want it done", "app.py: goal why not stored: %r" % goal["why"]
    f = _st_focus()
    assert f["item"] is not None and f["item"].get("parent_id") == gid, "app.py: focus should be the first child"
    first = None
    for it in STATE["items"]:
        if it.get("parent_id") == gid:
            first = it
            break
    assert first is not None and first.get("estimate_min") is not None and first["estimate_min"] <= 15, \
        "app.py: first step estimate should be <= 15"

    # bulk preview: free up saturday
    _st_reset("2026-10-20T08:00", [
        {"id": "bike", "kind": "task", "title": "Bike repair", "start": "2026-10-24T10:00",
         "end": "2026-10-24T11:00", "status": "open", "source": "seed"},
        {"id": "pic", "kind": "task", "title": "Picnic", "start": "2026-10-24T13:00",
         "end": "2026-10-24T15:00", "status": "open", "source": "seed"},
        {"id": "choir", "kind": "task", "title": "Choir", "start": "2026-10-25T10:00",
         "end": "2026-10-25T11:30", "status": "open", "source": "seed"},
        {"id": "demo", "kind": "task", "title": "Demo", "start": "2026-10-23T15:00",
         "end": "2026-10-23T16:00", "status": "open", "source": "seed"},
    ])
    before_ids = [it["id"] for it in STATE["items"]]
    r = _st_cmd("free up my saturday, I have a family reunion")
    assert isinstance(r, dict) and r.get("plan"), "v2.py: free up saturday should return a plan"
    plan = r["plan"]
    assert plan["status"] == "preview", "v2.py: plan should be preview, got %r" % plan["status"]
    assert sorted(plan["affected"]) == sorted(["bike", "pic"]), "v2.py: affected wrong: %r" % plan["affected"]
    creates = [op for op in plan["ops"] if op["op"] == "create"]
    assert len(creates) == 1, "v2.py: should be exactly one create op, got %d" % len(creates)
    assert "reunion" in (creates[0]["after"]["title"] or "").lower(), "v2.py: create title should contain reunion"
    assert [it["id"] for it in STATE["items"]] == before_ids, "v2.py: preview should not change items"
    _st_apply(plan["id"])
    ids_now = [it["id"] for it in STATE["items"]]
    assert "bike" not in ids_now and "pic" not in ids_now, "v2.py: apply should remove saturday items"
    assert any("reunion" in (it["title"] or "").lower() for it in STATE["items"]), "v2.py: apply should add reunion"
    _st_undo_plan(plan["id"])
    ids_now = [it["id"] for it in STATE["items"]]
    assert "bike" in ids_now and "pic" in ids_now, "v2.py: undo should restore saturday items"

    # nothing on sunday
    _st_reset("2026-10-20T08:00", [
        {"id": "choir", "kind": "task", "title": "Choir", "start": "2026-10-25T10:00",
         "end": "2026-10-25T11:30", "status": "open", "source": "seed"},
    ])
    r = _st_cmd("nothing on sunday please, I'm travelling to Lisbon")
    assert isinstance(r, dict) and r.get("plan"), "v2.py: sunday clear should return a plan"
    plan = r["plan"]
    assert plan["affected"] == ["choir"], "v2.py: sunday affected wrong: %r" % plan["affected"]
    creates = [op for op in plan["ops"] if op["op"] == "create"]
    assert len(creates) == 1 and "lisbon" in (creates[0]["after"]["title"] or "").lower(), \
        "v2.py: sunday create should contain Lisbon"

    # wipe friday evening
    _st_reset("2026-10-20T08:00", [
        {"id": "st", "kind": "task", "title": "Standup", "start": "2026-10-23T09:00",
         "end": "2026-10-23T09:30", "status": "open", "source": "seed"},
        {"id": "dr", "kind": "task", "title": "Drinks", "start": "2026-10-23T18:30",
         "end": "2026-10-23T20:00", "status": "open", "source": "seed"},
        {"id": "la", "kind": "task", "title": "Laundry", "start": "2026-10-23T21:00",
         "end": "2026-10-23T22:00", "status": "open", "source": "seed"},
    ])
    r = _st_cmd("wipe friday evening, I got concert tickets")
    assert isinstance(r, dict) and r.get("plan"), "v2.py: wipe friday should return a plan"
    plan = r["plan"]
    assert sorted(plan["affected"]) == sorted(["dr", "la"]), "v2.py: friday affected wrong: %r" % plan["affected"]
    creates = [op for op in plan["ops"] if op["op"] == "create"]
    assert len(creates) == 1 and "oncert" in (creates[0]["after"]["title"] or ""), \
        "v2.py: friday create should contain concert"

    # query the 23rd
    _st_reset("2026-10-20T08:00", [
        {"id": "eye", "kind": "event", "title": "Eye exam", "start": "2026-10-23T14:00",
         "end": "2026-10-23T14:45", "status": "open", "source": "seed"},
        {"id": "pia", "kind": "event", "title": "Piano", "start": "2026-10-23T17:00",
         "end": "2026-10-23T18:00", "status": "open", "source": "seed"},
    ])
    before = [dict(it) for it in STATE["items"]]
    r = _st_cmd("anything on the 23rd?")
    _st_check_no_bad(r)
    txt = " ".join(m.get("text") or "" for m in r.get("messages", []))
    assert "Eye exam" in txt and "Piano" in txt, "v2.py: query should name both items: %r" % txt
    assert [it["id"] for it in STATE["items"]] == [b["id"] for b in before], "v2.py: query should not change items"
    r = _st_cmd("push the eye exam back a week")
    eye = find_item("eye")
    assert eye["start"] == "2026-10-30T14:00", "v2.py: push back a week wrong: %r" % eye["start"]

    # v3 critic / design
    _st_reset("2026-10-19T09:00", [])
    c = _st_critic({"root": "r", "elements": {"r": {"type": "SpiralClock", "props": {}, "children": []}}})
    assert c["ok"] is False, "v3.py: SpiralClock should be rejected"
    assert any("SpiralClock" in p for p in c["problems"]), "v3.py: problem should name SpiralClock"
    d = _st_design("evening_work")
    assert len(d["drafts"]) >= 2, "v3.py: design should return at least 2 drafts"
    chosen = d["drafts"][d["chosen"]]
    cc = _st_critic(chosen)
    assert cc["ok"] is True, "v3.py: chosen draft should pass the critic: %r" % cc["problems"]

    # clock postpone
    _st_reset("2026-10-19T09:00", [
        {"id": "h0", "kind": "task", "title": "Old done 0", "start": "2026-10-13T20:15",
         "end": "2026-10-13T20:45", "status": "done", "source": "seed"},
        {"id": "t1", "kind": "task", "title": "Write cover letter", "start": "2026-10-19T14:00",
         "end": "2026-10-19T15:00", "status": "open", "source": "seed"},
    ])
    r = _st_clock("2026-10-19T16:00")
    _st_check_no_bad(r)
    post = [m for m in r["messages"] if m.get("kind") == "postponed"]
    assert len(post) == 1, "app.py: clock should postpone one task, got %d" % len(post)
    assert post[0].get("undo_id"), "app.py: postponed message should have an undo_id"
    assert "Write cover letter" in post[0]["text"], "app.py: postponed text should name the task"

    # chat: hmm then yes
    _st_reset("2026-10-19T09:00", [])
    r = _st_cmd("work on the pitch deck")
    _st_check_no_bad(r)
    gid = None
    for m in r["messages"]:
        if m.get("kind") == "question" and m.get("item_id"):
            gid = m["item_id"]
    assert gid is not None, "app.py: pitch deck should ask a question"
    r = _st_chat(gid, "hmm, what do you mean?")
    _st_check_no_bad(r)
    q = [m for m in r["messages"] if m.get("kind") == "question"]
    assert len(q) == 1, "app.py: hmm should get exactly one question"
    goal = find_item(gid)
    assert goal["why"] is None, "app.py: why should still be None after hmm"
    assert not any(m.get("kind") == "breakdown" for m in r["messages"]), "app.py: no breakdown without yes"
    _st_chat(gid, "yes")
    _st_chat(gid, "I want it clear")
    _st_chat(gid, "a clean deck")
    _st_chat(gid, "friday")
    r = _st_chat(gid, "what is step 2 about?")
    _st_check_no_bad(r)
    kids = [it for it in STATE["items"] if it.get("parent_id") == gid]
    assert len(kids) == 0, "app.py: no children before accept"
    r = _st_chat(gid, "ok")
    _st_check_no_bad(r)
    kids = [it for it in STATE["items"] if it.get("parent_id") == gid]
    assert len(kids) > 0, "app.py: ok should create children"

    # old task catch-up
    _st_reset("2026-10-19T09:00", [
        {"id": "old", "kind": "task", "title": "Old task", "start": "2026-10-18T10:00",
         "end": "2026-10-18T11:00", "status": "open", "source": "seed"},
    ])
    r = _st_clock("2026-10-19T09:30")
    _st_check_no_bad(r)
    post = [m for m in r["messages"] if m.get("kind") == "postponed"]
    assert len(post) == 1, "app.py: old task should be postponed, got %d" % len(post)
    assert "Old task" in post[0]["text"], "app.py: postponed should name Old task"
    old = find_item("old")
    s = parse_iso(old["start"])
    assert s is not None and s > parse_iso("2026-10-19T09:30"), "app.py: new start should be after 09:30"

    # three tasks with postpone_count 2
    _st_reset("2026-10-19T09:00", [
        {"id": "p1", "kind": "task", "title": "Task one", "start": "2026-10-19T13:00",
         "end": "2026-10-19T14:00", "status": "open", "source": "seed", "postpone_count": 2},
        {"id": "p2", "kind": "task", "title": "Task two", "start": "2026-10-19T13:00",
         "end": "2026-10-19T14:00", "status": "open", "source": "seed", "postpone_count": 2},
        {"id": "p3", "kind": "task", "title": "Task three", "start": "2026-10-19T13:00",
         "end": "2026-10-19T14:00", "status": "open", "source": "seed", "postpone_count": 2},
    ])
    r = _st_clock("2026-10-19T16:00")
    _st_check_no_bad(r)
    ci = [m for m in r["messages"] if m.get("kind") == "checkin"]
    assert len(ci) <= 1, "app.py: at most one checkin per clock, got %d" % len(ci)

    _st_reset("2026-10-19T09:00", [])


# === SECTION: main ===

def main():
    global DATA_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        try:
            with LOCK:
                selftest()
        except Exception:
            traceback.print_exc()
            sys.exit(1)
        sys.exit(0)

    DATA_DIR = args.data or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
