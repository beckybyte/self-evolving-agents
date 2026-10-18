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

# === SECTION: constants ===

ISO = "%Y-%m-%dT%H:%M"

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot", "lazy", "urgent", "alert"]

KIND_REPLACEMENTS = [
    ("overdue", "still open"),
    ("fail", "not finish"),
    ("miss", "skip"),
    ("late", "later on"),
    ("behind", "at your own pace"),
    ("should have", "could"),
    ("forgot", "didn't get to"),
    ("lazy", "tired"),
    ("urgent", "important"),
    ("alert", "note"),
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

WINDOWS = [
    ("around noon", 12, 0, 13, 0),
    ("noon", 12, 0, 13, 0),
    ("lunch", 12, 0, 13, 0),
    ("this afternoon", 14, 0, 17, 0),
    ("afternoon", 14, 0, 17, 0),
    ("morning", 9, 0, 12, 0),
    ("evening", 18, 0, 21, 0),
    ("tonight", 20, 0, 22, 0),
]

BIG_PHRASES = [
    "work on", "figure out", "deal with", "sort out", "get together",
    "organize", "plan", "prepare", "project", "portfolio", "pitch deck",
    "thesis", "website", "garage", "move", "taxes",
]

BIG_WORDS = [
    "organize", "plan", "prepare", "project", "portfolio", "thesis",
    "website", "garage", "move", "taxes",
]

YES_WORDS = ["yes", "yeah", "yep", "sure", "ok", "okay", "please", "looks good",
             "sounds good", "great", "perfect", "do it", "go ahead"]

NO_WORDS = ["no", "nope", "not now", "not right now", "no thanks", "later"]

STEP_VERBS = ["List", "Pick", "Write", "Open", "Collect", "Choose", "Draft", "Email",
              "Call", "Book", "Clear", "Sort", "Find", "Gather", "Send", "Review",
              "Update", "Upload", "Read"]

# === SECTION: state ===

STATE = {
    "now": "2026-10-19T09:00",
    "items": [],
    "messages": [],
    "convo": {},
    "undo": {},
    "sent": {},
}

LOCK = threading.RLock()

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = None

_id_counter = [0]


def new_id(prefix="m"):
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


def combine_d(d, h, m):
    return datetime.combine(d, dtime(h, m))


# === SECTION: wording check ===

def clean_text(text):
    if text is None:
        return ""
    out = str(text)
    low = out.lower()
    for bad, good in KIND_REPLACEMENTS:
        if bad in low:
            out = re.sub(re.escape(bad), good, out, flags=re.IGNORECASE)
            low = out.lower()
    return out


# === SECTION: store ===

def default_item(raw):
    it = {
        "id": raw.get("id") or new_id("i"),
        "kind": raw.get("kind") or "task",
        "title": raw.get("title") or "",
        "start": raw.get("start"),
        "end": raw.get("end"),
        "window": raw.get("window"),
        "status": raw.get("status") or "open",
        "why": raw.get("why"),
        "parent_id": raw.get("parent_id"),
        "estimate_min": raw.get("estimate_min"),
        "postpone_count": raw.get("postpone_count") or 0,
        "source": raw.get("source") or "seed",
    }
    return it


def find_item(item_id):
    for it in STATE["items"]:
        if it["id"] == item_id:
            return it
    return None


def add_agent(kind, text, item_id=None, undo_id=None, steps=None):
    msg = {
        "id": new_id("m"),
        "role": "agent",
        "kind": kind,
        "text": clean_text(text),
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


def save_state():
    if not DATA_DIR:
        return
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "state.json")
        with open(path, "w") as f:
            json.dump(STATE, f)
    except Exception:
        pass


# === SECTION: parsing ===

def strip_date_words(text):
    """Return (date_d or None, remaining_text)."""
    low = text.lower()
    base_d = now_dt().date()

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, text[:m.start()] + " " + text[m.end():]
        except Exception:
            pass

    m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
    if m and m.group(1).lower() in MONTHS:
        try:
            d = date(base_d.year, MONTHS[m.group(1).lower()], int(m.group(2)))
            return d, text[:m.start()] + " " + text[m.end():]
        except Exception:
            pass

    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\b", text)
    if m and m.group(2).lower() in MONTHS:
        try:
            d = date(base_d.year, MONTHS[m.group(2).lower()], int(m.group(1)))
            return d, text[:m.start()] + " " + text[m.end():]
        except Exception:
            pass

    m = re.search(r"\b(today|tomorrow)\b", low)
    if m:
        word = m.group(1)
        d = base_d if word == "today" else base_d + timedelta(days=1)
        return d, text[:m.start()] + " " + text[m.end():]

    for name, wd in WEEKDAYS.items():
        m = re.search(r"\b" + name + r"\b", low)
        if m:
            delta = (wd - base_d.weekday()) % 7
            if delta == 0:
                delta = 7
            d = base_d + timedelta(days=delta)
            return d, text[:m.start()] + " " + text[m.end():]

    return None, text


def strip_time_words(text):
    """Return (kind, start_tuple, end_tuple, window, remaining_text)."""
    low = text.lower()

    for phrase, h1, m1, h2, m2 in WINDOWS:
        m = re.search(r"\b" + re.escape(phrase) + r"\b", low)
        if m:
            rest = text[:m.start()] + " " + text[m.end():]
            return "window", (h1, m1), (h2, m2), phrase, rest

    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        ap = m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        rest = text[:m.start()] + " " + text[m.end():]
        return "clock", (h, mi), None, None, rest

    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1))
        ap = m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        rest = text[:m.start()] + " " + text[m.end():]
        return "clock", (h, 0), None, None, rest

    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        rest = text[:m.start()] + " " + text[m.end():]
        return "clock", (h, 0), None, None, rest

    return None, None, None, None, text


def clean_title(text):
    t = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    t = re.sub(r"^(maybe|perhaps)\s+", "", t, flags=re.IGNORECASE)
    t = t.strip(" ?")
    if not t:
        return "New task"
    return t[0].upper() + t[1:]


def parse_item_text(text, source="typed"):
    """Parse a single fragment into an item dict (not yet added)."""
    raw = text.strip()
    if not raw:
        return None

    is_note = raw.lower().startswith("maybe ") or raw.strip().endswith("?")

    date_d, rest = strip_date_words(raw)
    tkind, t1, t2, window, rest2 = strip_time_words(rest)

    title = clean_title(rest2)

    item = {
        "id": new_id("i"),
        "kind": "task",
        "title": title,
        "start": None,
        "end": None,
        "window": None,
        "status": "open",
        "why": None,
        "parent_id": None,
        "estimate_min": None,
        "postpone_count": 0,
        "source": source,
    }

    if tkind is None and date_d is None:
        item["kind"] = "note" if is_note else "task"
        return item

    if date_d is None:
        base_d = now_dt().date()
        if tkind == "clock":
            h, mi = t1
            cand = combine_d(base_d, h, mi)
            if cand <= now_dt():
                date_d = base_d + timedelta(days=1)
            else:
                date_d = base_d
        else:
            date_d = base_d

    if tkind == "clock":
        h, mi = t1
        start_dt = combine_d(date_d, h, mi)
        end_dt = start_dt + timedelta(minutes=60)
        item["kind"] = "event"
        item["start"] = fmt_iso(start_dt)
        item["end"] = fmt_iso(end_dt)
        item["window"] = None
    elif tkind == "window":
        h1, m1 = t1
        h2, m2 = t2
        start_dt = combine_d(date_d, h1, m1)
        end_dt = combine_d(date_d, h2, m2)
        item["window"] = window
        item["start"] = fmt_iso(start_dt)
        item["end"] = fmt_iso(end_dt)
        low = raw.lower()
        if any(w in low for w in ["with", "meet", "lunch", "dinner", "call with"]):
            item["kind"] = "event"
        else:
            item["kind"] = "task"
    else:
        item["kind"] = "note" if is_note else "task"

    return item


def is_big(text, estimate_min=None):
    if estimate_min is not None and estimate_min > 120:
        return True
    low = " " + text.lower() + " "
    for phrase in BIG_PHRASES:
        if " " in phrase:
            if phrase in low:
                return True
        else:
            if re.search(r"\b" + re.escape(phrase) + r"\b", low):
                return True
    return False


# === SECTION: reset / state ===

def do_reset(body):
    STATE["items"] = []
    STATE["messages"] = []
    STATE["convo"] = {}
    STATE["undo"] = {}
    STATE["sent"] = {}
    STATE["now"] = body.get("now") or "2026-10-19T09:00"

    for raw in body.get("items") or []:
        STATE["items"].append(default_item(raw))

    v2.init_v2(STATE)

    add_agent("info", "Hi, I'm here whenever you want to plan a little.")
    save_state()
    return state_response()


def state_response():
    return {
        "now": STATE["now"],
        "items": STATE["items"],
        "messages": STATE["messages"],
        "view": v2.view_state(STATE),
        "meta": v2.meta_state(STATE),
    }


# === SECTION: command ===

def do_command(body):
    text = body.get("text") or ""
    mode = body.get("mode") or "command"

    result = v2.handle_command(STATE, text)
    if result is not None:
        v2.tick(STATE, False)
        save_state()
        return result

    created = []
    messages = []

    if mode == "dump":
        fragments = re.split(r"[,;\n]+", text)
        titles = []
        first_big = None
        for frag in fragments:
            frag = frag.strip()
            if not frag:
                continue
            item = parse_item_text(frag, source="dump")
            if item is None:
                continue
            STATE["items"].append(item)
            created.append(item["id"])
            titles.append(item["title"])
            v2.add_log(STATE, "create", [item["id"]], {"title": item["title"]}, "dump")
            if first_big is None and is_big(item["title"], item["estimate_min"]):
                first_big = item
        if titles:
            messages.append(add_agent("info", "Added: " + ", ".join(titles) + "."))
        if first_big is not None:
            STATE["convo"][first_big["id"]] = "ask_split"
            messages.append(add_agent(
                "question",
                "This one sounds big. Want me to break '%s' into small steps?" % first_big["title"],
                item_id=first_big["id"],
            ))
    else:
        item = parse_item_text(text, source="typed")
        if item is not None:
            STATE["items"].append(item)
            created.append(item["id"])
            v2.add_log(STATE, "create", [item["id"]], {"title": item["title"]}, "typed")
            messages.append(add_agent("info", "Added '%s'." % item["title"]))
            if is_big(item["title"], item["estimate_min"]):
                STATE["convo"][item["id"]] = "ask_split"
                messages.append(add_agent(
                    "question",
                    "This one sounds big. Want me to break '%s' into small steps?" % item["title"],
                    item_id=item["id"],
                ))

    v2.tick(STATE, False)
    save_state()
    return {"messages": messages, "created": created}


# === SECTION: breakdown builder ===

def build_steps(goal_title):
    low = goal_title.lower()
    if "portfolio" in low:
        return [
            {"title": "List the projects you are proudest of", "estimate_min": 10},
            {"title": "Pick the five best projects", "estimate_min": 30},
            {"title": "Write a short note for each project", "estimate_min": 60},
            {"title": "Upload the projects to a simple website", "estimate_min": 60},
        ]
    return [
        {"title": "Open a note and list what '%s' needs" % goal_title, "estimate_min": 10},
        {"title": "Pick the first piece to start", "estimate_min": 20},
        {"title": "Draft the first piece", "estimate_min": 45},
        {"title": "Review it and send or save it", "estimate_min": 30},
    ]


def build_smaller_steps(goal_title):
    steps = build_steps(goal_title)
    out = []
    for s in steps[:5]:
        est = s["estimate_min"]
        if est > 30:
            est = 30
        out.append({"title": s["title"], "estimate_min": est})
    return out


def usual_hour():
    hours = []
    for it in STATE["items"]:
        if it.get("status") == "done" and it.get("start"):
            dt = parse_iso(it["start"])
            if dt is not None:
                hours.append(dt.hour)
    if not hours:
        return 19
    hours.sort()
    mid = hours[(len(hours) - 1) // 2]
    return mid


def overlaps(a_start, a_end, b_start, b_end):
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    return a_start < b_end and b_start < a_end


def free_slot(day_d, length_min, prefer_hour):
    """Find a free slot on day_d starting at or after prefer_hour, else from 08:00."""
    def try_from(start_h, start_m):
        t = combine_d(day_d, start_h, start_m)
        limit = combine_d(day_d, 23, 30)
        while t <= limit:
            t_end = t + timedelta(minutes=length_min)
            if t_end.date() != day_d:
                break
            busy = False
            for it in STATE["items"]:
                if it.get("status") != "open":
                    continue
                s = parse_iso(it.get("start"))
                e = parse_iso(it.get("end"))
                if overlaps(t, t_end, s, e):
                    busy = True
                    break
            if not busy:
                return t
            t = t + timedelta(minutes=15)
        return None

    slot = try_from(prefer_hour, 0)
    if slot is not None:
        return slot
    return try_from(8, 0)


def schedule_children(goal, steps, deadline_d):
    """Create child tasks for the goal, one per day, before deadline_d."""
    hour = usual_hour()
    base_d = now_dt().date()
    day_d = base_d
    # if today's slot has passed, start tomorrow
    first_start = combine_d(day_d, hour, 0)
    if first_start <= now_dt():
        day_d = base_d + timedelta(days=1)

    created = []
    for step in steps:
        est = step.get("estimate_min") or 30
        placed = None
        scan_d = day_d
        guard = 0
        while guard < 60:
            guard += 1
            if deadline_d is not None and scan_d >= deadline_d:
                break
            slot = free_slot(scan_d, est, hour)
            if slot is not None:
                placed = slot
                day_d = scan_d + timedelta(days=1)
                break
            scan_d = scan_d + timedelta(days=1)

        if placed is None:
            # place back to back on the last day before the deadline
            if deadline_d is not None:
                last_d = deadline_d - timedelta(days=1)
            else:
                last_d = day_d
            placed = combine_d(last_d, hour, 0)
            # avoid overlapping existing items by pushing forward
            guard2 = 0
            while guard2 < 60:
                guard2 += 1
                p_end = placed + timedelta(minutes=est)
                busy = False
                for it in STATE["items"]:
                    if it.get("status") != "open":
                        continue
                    s = parse_iso(it.get("start"))
                    e = parse_iso(it.get("end"))
                    if overlaps(placed, p_end, s, e):
                        busy = True
                        break
                if not busy:
                    break
                placed = placed + timedelta(minutes=est)

        child = {
            "id": new_id("i"),
            "kind": "task",
            "title": step["title"],
            "start": fmt_iso(placed),
            "end": fmt_iso(placed + timedelta(minutes=est)),
            "window": None,
            "status": "open",
            "why": None,
            "parent_id": goal["id"],
            "estimate_min": est,
            "postpone_count": 0,
            "source": "agent",
        }
        STATE["items"].append(child)
        created.append(child)
    return created


# === SECTION: chat ===

def is_yes(text):
    low = text.lower()
    for w in NO_WORDS:
        if w in low:
            return False
    for w in YES_WORDS:
        if w in low:
            return True
    return False


def is_no(text):
    low = text.lower()
    for w in NO_WORDS:
        if w in low:
            return True
    return False


def do_chat(body):
    item_id = body.get("item_id")
    text = body.get("text") or ""
    item = find_item(item_id)
    if item is None:
        raise KeyError(item_id)

    add_person(text, item_id)

    state = STATE["convo"].get(item_id)
    messages = []

    if state == "ask_split":
        if is_no(text):
            STATE["convo"][item_id] = None
            messages.append(add_agent("info", "Okay, I'll keep it as one task.", item_id=item_id))
        else:
            STATE["convo"][item_id] = "ask_why"
            messages.append(add_agent("question", "Why does this matter to you?", item_id=item_id))

    elif state == "ask_why":
        item["why"] = text
        STATE["convo"][item_id] = "ask_done"
        messages.append(add_agent("question", "What does done look like for you?", item_id=item_id))

    elif state == "ask_done":
        STATE["convo"][item_id] = "ask_deadline"
        messages.append(add_agent("question", "Is there a deadline, a day you'd like it finished by?", item_id=item_id))

    elif state == "ask_deadline":
        deadline_d, _rest = strip_date_words(text)
        if deadline_d is None:
            deadline_d = (now_dt() + timedelta(days=7)).date()
        STATE["convo"][item_id] = "propose"
        steps = build_steps(item["title"])
        messages.append(add_agent(
            "breakdown",
            "Here's a gentle plan for '%s'. Does this look ok?" % item["title"],
            item_id=item_id,
            steps=steps,
        ))

    elif state == "propose":
        if is_yes(text):
            steps = build_steps(item["title"])
            deadline_d = (now_dt() + timedelta(days=7)).date()
            children = schedule_children(item, steps, deadline_d)
            for c in children:
                v2.add_log(STATE, "create", [c["id"]], {"title": c["title"]}, "agent")
            STATE["convo"][item_id] = None
            first = steps[0]["title"] if steps else "the first step"
            messages.append(add_agent("info", "Done, the first tiny step is '%s'." % first, item_id=item_id))
        else:
            steps = build_smaller_steps(item["title"])
            messages.append(add_agent(
                "breakdown",
                "Here's a smaller plan for '%s'. Does this look ok?" % item["title"],
                item_id=item_id,
                steps=steps,
            ))

    elif state == "checkin":
        low = text.lower()
        if any(w in low for w in ["big", "smaller", "break", "too much"]):
            STATE["convo"][item_id] = "propose"
            steps = build_steps(item["title"])
            messages.append(add_agent(
                "breakdown",
                "Here's a gentle plan for '%s'. Does this look ok?" % item["title"],
                item_id=item_id,
                steps=steps,
            ))
        elif any(w in low for w in ["time", "busy", "wrong"]):
            end_dt = parse_iso(item.get("end"))
            length = 60
            if end_dt is not None:
                start_dt = parse_iso(item.get("start"))
                if start_dt is not None:
                    length = int((end_dt - start_dt).total_seconds() // 60) or 60
            base_d = now_dt().date() + timedelta(days=1)
            slot = free_slot(base_d, length, usual_hour())
            if slot is None:
                slot = combine_d(base_d, usual_hour(), 0)
            undo_id = new_id("u")
            STATE["undo"][undo_id] = {
                "item_id": item["id"],
                "start": item.get("start"),
                "end": item.get("end"),
                "window": item.get("window"),
                "postpone_count": item.get("postpone_count"),
            }
            item["start"] = fmt_iso(slot)
            item["end"] = fmt_iso(slot + timedelta(minutes=length))
            STATE["convo"][item_id] = None
            v2.add_log(STATE, "update", [item_id], {"moved": item["start"]}, "person")
            messages.append(add_agent(
                "info",
                "No worries, I moved '%s' to %s at %s." % (item["title"], slot.strftime("%a %b %d"), slot.strftime("%H:%M")),
                item_id=item_id,
                undo_id=undo_id,
            ))
        elif any(w in low for w in ["not needed", "drop", "remove", "don't need"]):
            STATE["convo"][item_id] = "confirm_drop"
            messages.append(add_agent("question", "Shall I remove '%s' for you?" % item["title"], item_id=item_id))
        else:
            STATE["convo"][item_id] = None
            messages.append(add_agent("info", "Thanks for telling me.", item_id=item_id))

    elif state == "confirm_drop":
        if is_yes(text):
            STATE["items"] = [it for it in STATE["items"] if it["id"] != item_id]
            STATE["convo"][item_id] = None
            v2.add_log(STATE, "delete", [item_id], {"title": item["title"]}, "person")
            messages.append(add_agent("info", "Okay, I've let '%s' go." % item["title"]))
        else:
            STATE["convo"][item_id] = None
            messages.append(add_agent("info", "Okay, I'll keep '%s'." % item["title"], item_id=item_id))

    else:
        if is_no(text):
            messages.append(add_agent("info", "Okay, I'll keep it as one task.", item_id=item_id))
        else:
            messages.append(add_agent("info", "Thanks, I've noted that.", item_id=item_id))

    log_kind = "chat"
    if state in ("checkin", "confirm_drop"):
        log_kind = "checkin_answer"
    v2.add_log(STATE, log_kind, [item_id], {"text": text}, "person")

    v2.tick(STATE, False)
    save_state()
    return {"messages": messages}


# === SECTION: done / undo ===

def do_done(item_id):
    item = find_item(item_id)
    if item is None:
        raise KeyError(item_id)
    item["status"] = "done"
    v2.add_log(STATE, "done", [item_id], {"title": item["title"]}, "person")
    v2.tick(STATE, False)
    save_state()
    return {"item": item}


def do_undo(body):
    undo_id = body.get("undo_id")
    rec = STATE["undo"].get(undo_id)
    if rec is None:
        raise KeyError(undo_id)
    item = find_item(rec.get("item_id"))
    if item is not None:
        item["start"] = rec.get("start")
        item["end"] = rec.get("end")
        item["window"] = rec.get("window")
        item["postpone_count"] = rec.get("postpone_count")
        v2.add_log(STATE, "update", [item["id"]], {"undo": undo_id}, "person")
    v2.tick(STATE, False)
    save_state()
    return {"ok": True}


# === SECTION: focus ===

def do_focus():
    goals = [it for it in STATE["items"] if it["status"] == "open" and it.get("why")]
    goal_ids = set(g["id"] for g in goals)

    children = [it for it in STATE["items"]
                if it["status"] == "open" and it.get("parent_id") in goal_ids]

    item = None
    parent = None
    why = None

    if children:
        def sort_key(it):
            s = it.get("start")
            return (0, s) if s else (1, "")
        children_sorted = sorted(children, key=sort_key)
        item = children_sorted[0]
        parent = find_item(item.get("parent_id"))
        why = parent.get("why") if parent else None
    else:
        tasks = [it for it in STATE["items"]
                 if it["status"] == "open" and it["kind"] == "task"
                 and not any(c.get("parent_id") == it["id"] for c in STATE["items"])]
        if tasks:
            def sort_key2(it):
                s = it.get("start")
                return (0, s) if s else (1, "")
            tasks_sorted = sorted(tasks, key=sort_key2)
            item = tasks_sorted[0]
            parent = find_item(item.get("parent_id")) if item.get("parent_id") else None
            if parent and parent.get("why"):
                why = parent.get("why")
            else:
                why = item.get("why")

    return {"item": item, "parent": parent, "why": why, "action": "Start 5 minutes"}


# === SECTION: clock + scheduler ===

def do_clock(body):
    new = body.get("now")
    if not new:
        return {"messages": []}
    new_dt = parse_iso(new)
    cur_dt = now_dt()
    if new_dt is None or new_dt <= cur_dt:
        return {"messages": []}

    messages = []
    t = cur_dt + timedelta(minutes=1)
    guard = 0
    while t <= new_dt and guard < 200000:
        guard += 1
        STATE["now"] = fmt_iso(t)
        for it in list(STATE["items"]):
            if it.get("status") != "open":
                continue
            start_dt = parse_iso(it.get("start"))
            end_dt = parse_iso(it.get("end"))

            if start_dt is not None and start_dt == t:
                key = (it["id"], "start", it.get("start"))
                if key not in STATE["sent"]:
                    STATE["sent"][key] = True
                    msg = add_agent(
                        "reminder",
                        "It's time for '%s' (%s)." % (it["title"], t.strftime("%H:%M")),
                        item_id=it["id"],
                    )
                    messages.append(msg)
                    v2.add_log(STATE, "reminder", [it["id"]], {"start": it.get("start")}, "clock")

            if end_dt is not None and end_dt - timedelta(minutes=10) == t:
                key = (it["id"], "end", it.get("end"))
                if key not in STATE["sent"]:
                    STATE["sent"][key] = True
                    msg = add_agent(
                        "reminder",
                        "'%s' wraps up in 10 minutes." % it["title"],
                        item_id=it["id"],
                    )
                    messages.append(msg)
                    v2.add_log(STATE, "reminder", [it["id"]], {"end": it.get("end")}, "clock")

            if it.get("kind") == "task" and end_dt is not None and t == end_dt + timedelta(minutes=31):
                has_children = any(c.get("parent_id") == it["id"] for c in STATE["items"])
                if has_children:
                    continue
                if it.get("postpone_count", 0) >= 2:
                    key = (it["id"], "checkin", it.get("end"))
                    if key not in STATE["sent"]:
                        STATE["sent"][key] = True
                        STATE["convo"][it["id"]] = "checkin"
                        msg = add_agent(
                            "checkin",
                            "How did '%s' feel? If it's too big, I can break it into smaller steps, or find a better time, or we can let it go. What would help?" % it["title"],
                            item_id=it["id"],
                        )
                        messages.append(msg)
                        v2.add_log(STATE, "checkin_answer", [it["id"]], {"checkin": True}, "clock")
                else:
                    length = 60
                    if start_dt is not None:
                        length = int((end_dt - start_dt).total_seconds() // 60) or 60
                    next_d = end_dt.date() + timedelta(days=1)
                    slot = free_slot(next_d, length, usual_hour())
                    if slot is None:
                        slot = combine_d(next_d, 8, 0)
                    undo_id = new_id("u")
                    STATE["undo"][undo_id] = {
                        "item_id": it["id"],
                        "start": it.get("start"),
                        "end": it.get("end"),
                        "window": it.get("window"),
                        "postpone_count": it.get("postpone_count"),
                    }
                    it["start"] = fmt_iso(slot)
                    it["end"] = fmt_iso(slot + timedelta(minutes=length))
                    it["postpone_count"] = it.get("postpone_count", 0) + 1
                    msg = add_agent(
                        "postponed",
                        "No worries, I moved '%s' to %s at %s. Undo if you'd rather keep it." % (
                            it["title"], slot.strftime("%a %b %d"), slot.strftime("%H:%M")),
                        item_id=it["id"],
                        undo_id=undo_id,
                    )
                    messages.append(msg)
                    v2.add_log(STATE, "postponed", [it["id"]], {"start": it["start"]}, "clock")

        t = t + timedelta(minutes=1)

    STATE["now"] = new
    v2.tick(STATE, True)
    save_state()
    return {"messages": messages}


# === SECTION: v2 routes ===

def do_import(body):
    filename = body.get("filename") or ""
    content = body.get("content") or ""
    return v2.import_file(STATE, filename, content)


def do_plan_apply(plan_id):
    return v2.apply_plan(STATE, plan_id)


def do_plan_undo(plan_id):
    return v2.undo_plan(STATE, plan_id)


def do_learn():
    return v2.learn(STATE)


def do_proposal_action(proposal_id, action):
    return v2.proposal_action(STATE, proposal_id, action)


def do_view(body):
    return v2.set_view(STATE, body.get("type"))


def do_prompts_rollback(agent):
    return v2.rollback(STATE, agent)


# === SECTION: HTTP handler ===

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, obj):
        try:
            data = json.dumps(obj).encode("utf-8")
        except Exception:
            data = b'{"error": "serialization"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
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
                    return self._send(200, do_focus())
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
                return self._send(404, {"error": "not found"})
            except KeyError as e:
                return self._send(404, {"error": "not found: %s" % e})
            except Exception as e:
                return self._send(500, {"error": str(e)})

    def do_POST(self):
        with LOCK:
            try:
                path = self.path.split("?")[0]
                body = self._read_body()

                if path == "/api/reset":
                    return self._send(200, do_reset(body))
                if path == "/api/command":
                    return self._send(200, do_command(body))
                if path == "/api/chat":
                    return self._send(200, do_chat(body))
                if path == "/api/undo":
                    return self._send(200, do_undo(body))
                if path == "/api/clock":
                    return self._send(200, do_clock(body))
                if path == "/api/import":
                    return self._send(200, do_import(body))
                if path == "/api/learn":
                    return self._send(200, do_learn())
                if path == "/api/view":
                    return self._send(200, do_view(body))

                m = re.match(r"^/api/items/([^/]+)/done$", path)
                if m:
                    return self._send(200, do_done(m.group(1)))

                m = re.match(r"^/api/plan/([^/]+)/apply$", path)
                if m:
                    return self._send(200, do_plan_apply(m.group(1)))

                m = re.match(r"^/api/plan/([^/]+)/undo$", path)
                if m:
                    return self._send(200, do_plan_undo(m.group(1)))

                m = re.match(r"^/api/proposals/([^/]+)/([^/]+)$", path)
                if m:
                    return self._send(200, do_proposal_action(m.group(1), m.group(2)))

                m = re.match(r"^/api/prompts/([^/]+)/rollback$", path)
                if m:
                    return self._send(200, do_prompts_rollback(m.group(1)))

                return self._send(404, {"error": "not found"})
            except KeyError as e:
                return self._send(404, {"error": "not found: %s" % e})
            except Exception as e:
                return self._send(500, {"error": str(e)})

    def do_DELETE(self):
        with LOCK:
            try:
                path = self.path.split("?")[0]
                m = re.match(r"^/api/beliefs/([^/]+)$", path)
                if m:
                    return self._send(200, v2.delete_belief(STATE, m.group(1)))
                return self._send(404, {"error": "not found"})
            except KeyError as e:
                return self._send(404, {"error": "not found: %s" % e})
            except Exception as e:
                return self._send(500, {"error": str(e)})

    def _serve_index(self):
        path = os.path.join(APP_DIR, "index.html")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception:
            data = FALLBACK_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass


FALLBACK_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Evolving Calendar</title></head>
<body>
<h1>Evolving Calendar</h1>
<div id="focus"></div>
<div id="timeline"></div>
<div id="chat-log"></div>
<input id="chat-input" />
</body></html>
"""


# === SECTION: selftest ===

def selftest():
    seed = [
        {"id": "h0", "kind": "task", "title": "History 0", "start": "2026-10-13T20:15",
         "end": "2026-10-13T20:45", "status": "done", "source": "seed"},
        {"id": "h1", "kind": "task", "title": "History 1", "start": "2026-10-13T21:00",
         "end": "2026-10-13T21:40", "status": "done", "source": "seed"},
        {"id": "t1", "kind": "task", "title": "Write cover letter", "start": "2026-10-19T14:00",
         "end": "2026-10-19T15:00", "status": "open", "source": "seed"},
        {"id": "yo", "kind": "event", "title": "Yoga", "start": "2026-10-24T10:00",
         "end": "2026-10-24T11:00", "status": "open", "source": "seed"},
        {"id": "den", "kind": "event", "title": "Dentist", "start": "2026-10-22T15:00",
         "end": "2026-10-22T16:00", "status": "open", "source": "seed"},
    ]
    do_reset({"now": "2026-10-19T09:00", "items": seed})

    do_command({"text": "dentist thursday 3pm", "mode": "command"})
    do_command({"text": "lunch with Ana around noon tomorrow", "mode": "command"})
    do_command({"text": "call mom, portfolio, maybe yoga?", "mode": "dump"})
    r = do_command({"text": "get my portfolio together", "mode": "command"})
    goal_id = None
    for cid in r.get("created", []):
        it = find_item(cid)
        if it and "portfolio" in it["title"].lower():
            goal_id = cid
    if goal_id is None:
        for it in STATE["items"]:
            if "portfolio" in it["title"].lower() and it.get("source") == "typed":
                goal_id = it["id"]
                break

    if goal_id:
        do_chat({"item_id": goal_id, "text": "yes, please"})
        do_chat({"item_id": goal_id, "text": "I want to feel proud"})
        do_chat({"item_id": goal_id, "text": "Five projects online"})
        do_chat({"item_id": goal_id, "text": "By Friday, October 23."})
        do_chat({"item_id": goal_id, "text": "looks good"})

    do_focus()

    if goal_id:
        for it in STATE["items"]:
            if it.get("parent_id") == goal_id and it["status"] == "open":
                do_done(it["id"])
                break

    do_command({"text": "what's on thursday?", "mode": "command"})
    do_command({"text": "move my dentist to next week", "mode": "command"})
    r = do_command({"text": "cancel yoga", "mode": "command"})
    if isinstance(r, dict) and r.get("plan"):
        do_plan_apply(r["plan"]["id"])

    r = do_command({"text": "empty out my weekend for a business trip", "mode": "command"})
    if isinstance(r, dict) and r.get("plan"):
        pid = r["plan"]["id"]
        do_plan_apply(pid)
        do_plan_undo(pid)

    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\n"
        "BEGIN:VEVENT\nUID:ev1\nSUMMARY:Standup\nDTSTART;TZID=Europe/Paris:20261021T090000\nDTEND;TZID=Europe/Paris:20261021T093000\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:ev2\nSUMMARY:Holiday\nDTSTART;VALUE=DATE:20261025\nDTEND;VALUE=DATE:20261026\nEND:VEVENT\n"
        "END:VCALENDAR\n"
    )
    r = do_import({"filename": "cal.ics", "content": ics})
    if isinstance(r, dict) and r.get("plan"):
        do_plan_apply(r["plan"]["id"])

    r = do_import({"filename": "note.txt", "content": "I'll be away this weekend, please clear it. Also lunch with Ana on Wednesday around noon."})
    if isinstance(r, dict) and r.get("plan"):
        do_plan_apply(r["plan"]["id"])

    do_learn()
    do_view({"type": "workweek"})

    prop = v2.get_proposal(STATE).get("proposal")
    if prop:
        do_proposal_action(prop["id"], "accept")
        do_proposal_action(prop["id"], "undo")

    v2.get_prompts(STATE)
    do_prompts_rollback("designer")
    v2.get_activity(STATE)
    v2.get_log(STATE)

    r = do_clock({"now": "2026-10-19T16:00"})
    for m in r.get("messages", []):
        if m.get("kind") == "postponed" and m.get("undo_id"):
            do_undo({"undo_id": m["undo_id"]})
            break

    do_clock({"now": "2026-10-23T23:00"})

    if find_item("t1"):
        do_chat({"item_id": "t1", "text": "honestly it feels too big"})

    state_response()
    do_reset({"now": "2026-10-19T09:00", "items": []})


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

    DATA_DIR = args.data or os.path.join(os.getcwd(), "data")
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
