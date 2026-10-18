import argparse
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, date, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BANNED = [
    "overdue", "fail", "miss", "late", "behind", "should have", "forgot",
    "lazy", "urgent", "alert", "later", "dismiss", "relate",
]
KIND_REPLACEMENTS = {
    "overdue": "still open", "failed": "not finished", "missed": "not done yet",
    "late": "behind schedule", "behind": "running behind", "should have": "could",
    "forgot": "not remembered", "lazy": "taking it easy", "urgent": "important",
    "alert": "note", "later": "another time", "dismiss": "set aside",
    "relate": "connect",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
STATE = {
    "now": None,
    "items": [],
    "messages": [],
    "convo": {},
    "undo_records": {},
    "sent_reminders": set(),
    "postponed_marked": set(),
    "next_id": 1,
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
SOFT_WINDOWS = [
    ("around noon", "12:00", "13:00"), ("noon", "12:00", "13:00"),
    ("lunch", "12:00", "13:00"), ("morning", "09:00", "12:00"),
    ("afternoon", "14:00", "17:00"), ("this afternoon", "14:00", "17:00"),
    ("evening", "18:00", "21:00"), ("tonight", "20:00", "22:00"),
]
YES_WORDS = ["yes", "yeah", "yep", "sure", "ok", "okay", "please",
             "looks good", "sounds good", "great", "perfect", "do it", "go ahead"]
NO_WORDS = ["no", "nope", "not now", "not right now", "no thanks", "later"]
BIG_PATTERNS = [
    r"\bwork on\b", r"\bfigure out\b", r"\bdeal with\b", r"\bsort out\b",
    r"\bget .* together\b", r"\borganize\b", r"\bplan\b", r"\bprepare\b",
    r"\bproject\b", r"\bportfolio\b", r"\bpitch deck\b", r"\bthesis\b",
    r"\bwebsite\b", r"\bgarage\b", r"\bmove\b", r"\btaxes\b",
]
CONCRETE_VERBS = [
    "List", "Pick", "Write", "Open", "Collect", "Choose", "Draft", "Email",
    "Call", "Book", "Clear", "Sort", "Find", "Gather", "Send", "Review",
    "Update", "Upload", "Read",
]


def now_iso():
    if STATE["now"] is None:
        return None
    return STATE["now"].strftime("%Y-%m-%dT%H:%M")


def parse_iso(s):
    if s is None or s == "":
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M")


def fmt(dt):
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M")


def default_item():
    return {
        "id": None, "kind": "task", "title": "", "start": None, "end": None,
        "window": None, "status": "open", "why": None, "parent_id": None,
        "estimate_min": None, "postpone_count": 0, "source": "seed",
    }


def new_id():
    STATE["next_id"] += 1
    return "i%d" % (STATE["next_id"] - 1)


def new_message_id():
    STATE["next_id"] += 1
    return "m%d" % (STATE["next_id"] - 1)


def add_agent(kind, text, item_id=None, undo_id=None, steps=None):
    text = check_wording(text)
    msg = {
        "id": new_message_id(), "role": "agent", "kind": kind,
        "text": text, "item_id": item_id, "at": now_iso(),
        "undo_id": undo_id,
    }
    if steps is not None:
        msg["steps"] = steps
    STATE["messages"].append(msg)
    return msg


def add_person(text, item_id=None):
    msg = {
        "id": new_message_id(), "role": "person", "kind": "reply",
        "text": text, "item_id": item_id, "at": now_iso(), "undo_id": None,
    }
    STATE["messages"].append(msg)
    return msg


def check_wording(text):
    out = text
    low = out.lower()
    for bad in BANNED:
        if bad in low:
            out = KIND_REPLACEMENTS.get(bad, "kind note")
            break
    return out


def item_dict(item):
    d = default_item()
    d.update(item)
    return d


def find_item(item_id):
    for it in STATE["items"]:
        if it["id"] == item_id:
            return it
    return None


def item_index(item_id):
    for i, it in enumerate(STATE["items"]):
        if it["id"] == item_id:
            return i
    return -1


def usual_hour():
    hours = []
    for it in STATE["items"]:
        if it["status"] == "done" and it["start"] is not None:
            dt = parse_iso(it["start"])
            if dt is not None:
                hours.append(dt.hour)
    if not hours:
        return 19
    hours.sort()
    return hours[len(hours) // 2] // 1 * 1


def parse_date_word(text, ref):
    low = text.lower()
    for name, wd in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            d = ref.date()
            while d.weekday() != wd:
                d += timedelta(days=1)
            return d
    m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b"
        r"\s*(\d{1,2})", low)
    if m:
        mon = MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        try:
            d = date(ref.year, mon, day)
        except ValueError:
            return None
        if d < ref.date():
            d = date(ref.year + 1, mon, day)
        return d
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_time_word(text):
    low = text.lower()
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        h = int(m.group(1)); mi = int(m.group(2)); ampm = m.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        return time(h, mi)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1)); ampm = m.group(2)
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        return time(h, 0)
    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        return time(h, 0)
    return None


def parse_soft_window(text):
    low = text.lower()
    for phrase, st, en in SOFT_WINDOWS:
        if phrase in low:
            return phrase, st, en
    return None, None, None


def parse_command(text, ref):
    low = text.lower()
    d = parse_date_word(low, ref)
    t = parse_time_word(low)
    sw, st, en = parse_soft_window(low)
    title = low
    if d is not None:
        m = re.search(
            r"\b(january|february|march|april|may|june|july|august|september|"
            r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b"
            r"\s*\d{1,2}", title)
        if m:
            title = title.replace(m.group(0), " ")
        m = re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", title)
        if m:
            title = title.replace(m.group(0), " ")
    for name in WEEKDAYS:
        title = re.sub(r"\b" + name + r"\b", " ", title)
    if t is not None:
        title = re.sub(r"\b\d{1,2}:\d{2}\s*(am|pm)?\b", " ", title)
        title = re.sub(r"\b\d{1,2}\s*(am|pm)\b", " ", title)
        title = re.sub(r"\bat\s+\d{1,2}\b", " ", title)
    if sw is not None:
        title = title.replace(sw, " ")
    title = re.sub(r"\s+", " ", title).strip(" ,.?").strip()
    if title:
        title = title[0].upper() + title[1:]
    else:
        title = "Untitled"
    if t is not None and d is not None:
        start = datetime.combine(d, t)
        end = start + timedelta(minutes=60)
        return {"kind": "event", "title": title, "start": fmt(start),
                "end": fmt(end), "window": None, "date": d}
    if sw is not None and d is not None:
        st_dt = datetime.combine(d, parse_iso("1970-01-01T" + st).time())
        en_dt = datetime.combine(d, parse_iso("1970-01-01T" + en).time())
        return {"kind": "event", "title": title, "start": fmt(st_dt),
                "end": fmt(en_dt), "window": sw, "date": d}
    if t is not None and d is None:
        d = ref.date()
        start = datetime.combine(d, t)
        if start <= ref:
            start += timedelta(days=1)
        end = start + timedelta(minutes=60)
        return {"kind": "event", "title": title, "start": fmt(start),
                "end": fmt(end), "window": None, "date": start.date()}
    if sw is not None and d is None:
        d = ref.date()
        st_dt = datetime.combine(d, parse_iso("1970-01-01T" + st).time())
        en_dt = datetime.combine(d, parse_iso("1970-01-01T" + en).time())
        return {"kind": "event", "title": title, "start": fmt(st_dt),
                "end": fmt(en_dt), "window": sw, "date": d}
    if d is not None:
        return {"kind": "task", "title": title, "start": None, "end": None,
                "window": None, "date": d}
    start = ref
    end = ref + timedelta(minutes=60)
    return {"kind": "task", "title": title, "start": fmt(start),
            "end": fmt(end), "window": None, "date": ref.date()}


def is_big(text, estimate_min):
    low = text.lower()
    for pat in BIG_PATTERNS:
        if re.search(pat, low):
            return True
    if estimate_min is not None and estimate_min > 120:
        return True
    return False


def is_appointment(text):
    low = text.lower()
    for w in ["with", "meet", "lunch", "dinner", "call"]:
        if w in low:
            return True
    return False


def is_note(text):
    low = text.lower().strip()
    if low.startswith("maybe "):
        return True
    if low.endswith("?"):
        return True
    return False


def free_slot(day, length_min, after_hour, busy):
    t = datetime.combine(day, time(after_hour, 0))
    end_day = datetime.combine(day, time(23, 30))
    while t + timedelta(minutes=length_min) <= end_day:
        slot_end = t + timedelta(minutes=length_min)
        ok = True
        for it in busy:
            if it["start"] is None or it["end"] is None:
                continue
            s = parse_iso(it["start"]); e = parse_iso(it["end"])
            if s is None or e is None:
                continue
            if t < e and slot_end > s:
                ok = False
                break
        if ok:
            return t
        t += timedelta(minutes=15)
    t = datetime.combine(day, time(8, 0))
    end_day = datetime.combine(day, time(23, 30))
    while t + timedelta(minutes=length_min) <= end_day:
        slot_end = t + timedelta(minutes=length_min)
        ok = True
        for it in busy:
            if it["start"] is None or it["end"] is None:
                continue
            s = parse_iso(it["start"]); e = parse_iso(it["end"])
            if s is None or e is None:
                continue
            if t < e and slot_end > s:
                ok = False
                break
        if ok:
            return t
        t += timedelta(minutes=15)
    return None


def endpoint_reset(body):
    global STATE
    now = parse_iso(body.get("now")) if body else None
    if now is None:
        now = datetime(2026, 10, 19, 9, 0)
    items = []
    if body and isinstance(body.get("items"), list):
        for src in body["items"]:
            it = default_item()
            for k in it:
                if k in src:
                    it[k] = src[k]
            if it["id"] is None:
                it["id"] = new_id()
            items.append(it)
    STATE = {
        "now": now, "items": items, "messages": [],
        "convo": {}, "undo_records": {}, "sent_reminders": set(),
        "postponed_marked": set(), "next_id": STATE["next_id"],
    }
    add_agent("info", "Hi, I'm here whenever you want to plan a little.")
    return {"now": fmt(now), "items": STATE["items"], "messages": STATE["messages"]}


def endpoint_command(body):
    text = body.get("text", "") if body else ""
    mode = body.get("mode", "command") if body else "command"
    ref = STATE["now"] or datetime(2026, 10, 19, 9, 0)
    created = []
    messages = []
    if mode == "dump":
        parts = re.split(r"[,\n;]", text)
        first_big = None
        titles = []
        for frag in parts:
            frag = frag.strip()
            if not frag:
                continue
            t2 = frag
            if t2.lower().startswith("maybe "):
                t2 = t2[6:]
            t2 = t2.strip(" ?.")
            if not t2:
                continue
            parsed = parse_command(frag, ref)
            it = default_item()
            it["id"] = new_id()
            it["title"] = parsed["title"]
            it["kind"] = parsed["kind"]
            it["start"] = parsed.get("start")
            it["end"] = parsed.get("end")
            it["window"] = parsed.get("window")
            it["source"] = "dump"
            STATE["items"].append(it)
            created.append(it["id"])
            titles.append(it["title"])
            if first_big is None and is_big(frag, it["estimate_min"]):
                first_big = it
        msg_text = "I've noted: " + ", ".join(titles) + "."
        messages.append(add_agent("info", msg_text))
        if first_big is not None:
            if is_big(first_big["title"], first_big["estimate_min"]):
                q = "This one sounds big. Want me to break '%s' into small steps?" % first_big["title"]
                messages.append(add_agent("question", q, item_id=first_big["id"]))
                STATE["convo"][first_big["id"]] = "ask_split"
        return {"messages": messages, "created": created}
    parsed = parse_command(text, ref)
    it = default_item()
    it["id"] = new_id()
    it["title"] = parsed["title"]
    it["kind"] = parsed["kind"]
    it["start"] = parsed.get("start")
    it["end"] = parsed.get("end")
    it["window"] = parsed.get("window")
    it["source"] = "typed"
    STATE["items"].append(it)
    created.append(it["id"])
    messages = []
    if it["kind"] == "task" and is_big(text, it["estimate_min"]):
        q = "This one sounds big. Want me to break '%s' into small steps?" % it["title"]
        messages.append(add_agent("question", q, item_id=it["id"]))
        STATE["convo"][it["id"]] = "ask_split"
    else:
        when = ""
        if it["start"] is not None:
            dt = parse_iso(it["start"])
            when = " on " + dt.strftime("%a %b %d at %H:%M")
        elif it["window"]:
            when = " around " + it["window"]
        messages.append(add_agent("info", "Added '%s'%s." % (it["title"], when)))
    return {"messages": messages, "created": created}


def endpoint_chat(body):
    item_id = body.get("item_id") if body else None
    text = body.get("text", "") if body else ""
    it = find_item(item_id)
    if it is None:
        return {"error": "item not found", "not found": True}
    state = STATE["convo"].get(item_id)
    messages = []
    add_person(text, item_id)
    low = text.lower().strip()
    is_no = any(n in low for n in NO_WORDS)
    is_yes = any(y in low for y in YES_WORDS)
    if state == "ask_split":
        if is_no:
            messages.append(add_agent("info", "Okay, I'll keep it as one task."))
            STATE["convo"][item_id] = None
        else:
            messages.append(add_agent("question", "Why does this matter to you?", item_id=item_id))
            STATE["convo"][item_id] = "ask_why"
    elif state == "ask_why":
        it["why"] = text
        messages.append(add_agent("question", "What does done look like for you?", item_id=item_id))
        STATE["convo"][item_id] = "ask_done"
    elif state == "ask_done":
        messages.append(add_agent("question", "Is there a deadline, a day you'd like it finished by?", item_id=item_id))
        STATE["convo"][item_id] = "ask_deadline"
    elif state == "ask_deadline":
        ref = STATE["now"] or datetime(2026, 10, 19, 9, 0)
        d = parse_date_word(low, ref)
        if d is None:
            d = (ref + timedelta(days=7)).date()
        steps = build_steps(it["title"])
        msg_text = "Here's a gentle plan for '%s'. Does this look ok?" % it["title"]
        messages.append(add_agent("breakdown", msg_text, item_id=item_id, steps=steps))
        STATE["convo"][item_id] = {"phase": "propose", "deadline": d, "steps": steps}
    elif isinstance(state, dict) and state.get("phase") == "propose":
        if is_yes:
            steps = state["steps"]
            deadline = state["deadline"]
            day = STATE["now"].date()
            hour = usual_hour()
            placed = []
            for st in steps:
                start = free_slot(day, st["estimate_min"], hour, STATE["items"])
                if start is None or start.date() > deadline:
                    start = free_slot(deadline, st["estimate_min"], hour, STATE["items"])
                if start is None:
                    start = datetime.combine(deadline, time(hour, 0))
                child = default_item()
                child["id"] = new_id()
                child["title"] = st["title"]
                child["kind"] = "task"
                child["start"] = fmt(start)
                child["end"] = fmt(start + timedelta(minutes=st["estimate_min"]))
                child["window"] = None
                child["parent_id"] = item_id
                child["estimate_min"] = st["estimate_min"]
                child["source"] = "agent"
                STATE["items"].append(child)
                placed.append(child)
                day = start.date()
            first = placed[0] if placed else None
            if first:
                messages.append(add_agent("info", "Done, the first tiny step is '%s'." % first["title"]))
            else:
                messages.append(add_agent("info", "Done, the steps are ready."))
            STATE["convo"][item_id] = None
        else:
            steps = build_steps(it["title"], smaller=True)
            msg_text = "Here's a gentler plan for '%s'. Does this look ok?" % it["title"]
            messages.append(add_agent("breakdown", msg_text, item_id=item_id, steps=steps))
            STATE["convo"][item_id] = {"phase": "propose", "deadline": state["deadline"], "steps": steps}
    elif state == "checkin":
        if any(w in low for w in ["too big", "smaller", "break"]):
            steps = build_steps(it["title"])
            msg_text = "Here's a gentle plan for '%s'. Does this look ok?" % it["title"]
            messages.append(add_agent("breakdown", msg_text, item_id=item_id, steps=steps))
            STATE["convo"][item_id] = {"phase": "propose", "deadline": None, "steps": steps}
        elif any(w in low for w in ["time", "busy", "wrong"]):
            hour = usual_hour()
            if it["start"] is not None:
                cur = parse_iso(it["start"])
                day = cur.date() + timedelta(days=1)
            else:
                day = STATE["now"].date() + timedelta(days=1)
            length = (parse_iso(it["end"]) - parse_iso(it["start"])).total_seconds() / 60.0 if it["end"] and it["start"] else 60
            start = free_slot(day, int(length), hour, STATE["items"])
            if start is None:
                start = datetime.combine(day, time(hour, 0))
            it["start"] = fmt(start)
            it["end"] = fmt(start + timedelta(minutes=int(length)))
            it["postpone_count"] = 0
            STATE["convo"][item_id] = None
            messages.append(add_agent("info", "Moved '%s' to %s." % (it["title"], start.strftime("%a %b %d at %H:%M"))))
        elif any(w in low for w in ["not needed", "drop", "remove", "don't need", "do not need"]):
            messages.append(add_agent("question", "Shall I remove '%s' for you?" % it["title"], item_id=item_id))
            STATE["convo"][item_id] = "confirm_drop"
    elif state == "confirm_drop":
        if is_yes:
            idx = item_index(item_id)
            if idx >= 0:
                STATE["items"].pop(idx)
            messages.append(add_agent("info", "Removed '%s'." % it["title"]))
            STATE["convo"][item_id] = None
        else:
            messages.append(add_agent("info", "Kept '%s'." % it["title"]))
            STATE["convo"][item_id] = None
    else:
        messages.append(add_agent("info", "I'm listening."))
    return {"messages": messages}


def build_steps(title, smaller=False):
    low = title.lower()
    if "portfolio" in low:
        steps = [
            {"title": "List the projects you are proudest of", "estimate_min": 10},
            {"title": "Pick the five best projects", "estimate_min": 30},
            {"title": "Write a short note for each project", "estimate_min": 60},
            {"title": "Upload the projects to a simple website", "estimate_min": 60},
        ]
    else:
        steps = [
            {"title": "Open a note and list what '%s' needs" % title, "estimate_min": 10},
            {"title": "Pick the first piece to start", "estimate_min": 20},
            {"title": "Draft the first piece", "estimate_min": 45},
            {"title": "Review it and send or save it", "estimate_min": 30},
        ]
    if smaller:
        steps = steps[:3]
        for st in steps:
            if st["estimate_min"] > 45:
                st["estimate_min"] = 45
    return steps


def endpoint_items_done(item_id):
    it = find_item(item_id)
    if it is None:
        return {"error": "item not found", "not found": True}
    it["status"] = "done"
    return {"item": it}


def endpoint_undo(body):
    undo_id = body.get("undo_id") if body else None
    rec = STATE["undo_records"].get(undo_id)
    if rec is None:
        return {"ok": True}
    it = find_item(rec["item_id"])
    if it is None:
        return {"ok": True}
    it["start"] = rec.get("start")
    it["end"] = rec.get("end")
    it["window"] = rec.get("window")
    it["postpone_count"] = rec.get("postpone_count", 0)
    return {"ok": True}


def endpoint_clock(body):
    new = parse_iso(body.get("now")) if body else None
    if new is None:
        return {"error": "bad time"}
    if STATE["now"] is not None and new <= STATE["now"]:
        return {"messages": []}
    messages = []
    t = STATE["now"] + timedelta(minutes=1)
    while t <= new:
        for it in list(STATE["items"]):
            if it["status"] != "open":
                continue
            if it["start"] is not None:
                s = parse_iso(it["start"])
                if s is not None and s == t:
                    key = (it["id"], "start", it["start"])
                    if key not in STATE["sent_reminders"]:
                        STATE["sent_reminders"].add(key)
                        dt = parse_iso(it["start"])
                        messages.append(add_agent("reminder", "It's time for '%s' (%s)." % (it["title"], dt.strftime("%H:%M")), item_id=it["id"]))
            if it["end"] is not None:
                e = parse_iso(it["end"])
                if e is not None and e - timedelta(minutes=10) == t:
                    key = (it["id"], "end", it["end"])
                    if key not in STATE["sent_reminders"]:
                        STATE["sent_reminders"].add(key)
                        messages.append(add_agent("reminder", "'%s' wraps up in 10 minutes." % it["title"], item_id=it["id"]))
            if it["kind"] == "task" and it["end"] is not None:
                e = parse_iso(it["end"])
                if e is not None and t == e + timedelta(minutes=31):
                    if it["id"] in STATE["postponed_marked"]:
                        continue
                    children = [c for c in STATE["items"] if c["parent_id"] == it["id"] and c["status"] == "open"]
                    if children:
                        continue
                    if it["postpone_count"] >= 2:
                        STATE["postponed_marked"].add(it["id"])
                        messages.append(add_agent("checkin", "How did '%s' feel? If it's too big, I can break it into smaller steps, or find a better time, or we can let it go. What would help?" % it["title"], item_id=it["id"]))
                        STATE["convo"][it["id"]] = "checkin"
                        continue
                    rec = {
                        "item_id": it["id"], "start": it["start"], "end": it["end"],
                        "window": it["window"], "postpone_count": it["postpone_count"],
                    }
                    uid = new_message_id()
                    STATE["undo_records"][uid] = rec
                    day = e.date() + timedelta(days=1)
                    length = (e - parse_iso(it["start"])).total_seconds() / 60.0 if it["start"] else 60
                    hour = usual_hour()
                    start = free_slot(day, int(length), hour, STATE["items"])
                    if start is None:
                        start = datetime.combine(day, time(hour, 0))
                    it["start"] = fmt(start)
                    it["end"] = fmt(start + timedelta(minutes=int(length)))
                    it["postpone_count"] += 1
                    messages.append(add_agent("postponed", "No worries, I moved '%s' to %s at %s. Undo if you'd rather keep it." % (it["title"], start.strftime("%a %b %d"), start.strftime("%H:%M")), item_id=it["id"], undo_id=uid))
        t += timedelta(minutes=1)
    STATE["now"] = new
    return {"messages": messages}


def endpoint_focus():
    goals = [it for it in STATE["items"] if it["status"] == "open" and it["why"]]
    item = None
    parent = None
    why = None
    if goals:
        best = None
        for g in goals:
            kids = [c for c in STATE["items"] if c["parent_id"] == g["id"] and c["status"] == "open"]
            for c in kids:
                s = parse_iso(c["start"]) if c["start"] else None
                key = (s is None, s or datetime.max)
                if best is None or key < best[0]:
                    best = (key, c, g)
        if best:
            item = best[1]
            parent = best[2]
            why = parent.get("why")
    if item is None:
        best = None
        for it in STATE["items"]:
            if it["status"] == "open" and it["kind"] == "task":
                has_children = any(c["parent_id"] == it["id"] for c in STATE["items"])
                if has_children:
                    continue
                s = parse_iso(it["start"]) if it["start"] else None
                key = (s is None, s or datetime.max)
                if best is None or key < best[0]:
                    best = (key, it)
        if best:
            item = best[1]
            pid = item.get("parent_id")
            parent = find_item(pid) if pid else None
            why = item.get("why")
            if not why and parent:
                why = parent.get("why")
    return {
        "item": item, "parent": parent, "why": why,
        "action": "Start 5 minutes" if item else "Start 5 minutes",
    }


def endpoint_state():
    return {"now": fmt(STATE["now"]), "items": STATE["items"], "messages": STATE["messages"]}


ROUTES = [
    (re.compile(r"^/api/items/([^/]+)/done$"), "items_done"),
    (re.compile(r"^/api/command$"), "command"),
    (re.compile(r"^/api/chat$"), "chat"),
    (re.compile(r"^/api/undo$"), "undo"),
    (re.compile(r"^/api/clock$"), "clock"),
    (re.compile(r"^/api/focus$"), "focus"),
    (re.compile(r"^/api/state$"), "state"),
    (re.compile(r"^/api/reset$"), "reset"),
    (re.compile(r"^/health$"), "health"),
]


def handle(path, method, body):
    if method == "GET" and path == "/":
        return {"page": True}
    for rx, name in ROUTES:
        m = rx.match(path)
        if m:
            if name == "health":
                return {"ok": True}
            if name == "state":
                return endpoint_state()
            if name == "reset":
                return endpoint_reset(body or {})
            if name == "command":
                return endpoint_command(body or {})
            if name == "chat":
                return endpoint_chat(body or {})
            if name == "items_done":
                return endpoint_items_done(m.group(1))
            if name == "undo":
                return endpoint_undo(body or {})
            if name == "clock":
                return endpoint_clock(body or {})
            if name == "focus":
                return endpoint_focus()
    return {"error": "not found", "not found": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            with LOCK:
                result = handle(self.path, "GET", None)
            if "page" in result:
                html = self._index_html()
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if result.get("error"):
                if result.get("not found"):
                    self._send(404, result)
                else:
                    self._send(400, result)
            else:
                self._send(200, result)
        except Exception as e:
            traceback.print_exc()
            try:
                self._send(500, {"error": str(e)})
            except Exception:
                pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}
        try:
            with LOCK:
                result = handle(self.path, "POST", body)
            if result.get("error"):
                if result.get("not found"):
                    self._send(404, result)
                else:
                    self._send(400, result)
            else:
                self._send(200, result)
        except Exception as e:
            traceback.print_exc()
            try:
                self._send(500, {"error": str(e)})
            except Exception:
                pass

    def _index_html(self):
        path = os.path.join(BASE_DIR, "index.html")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return """<html><head><meta charset="utf-8"><title>Evolving Calendar</title></head>
<body>
<h2>Evolving Calendar</h2>
<input id="chat-input" placeholder="Type something..." />
<button onclick="send()">Send</button>
<div id="chat-log"></div>
<div id="focus"></div>
<div id="timeline"></div>
<script>
async function send(){
  const t=document.getElementById('chat-input').value;
  await fetch('/api/chat',{method:'POST',body:JSON.stringify({item_id:null,text:t})});
  document.getElementById('chat-input').value='';
  load();
}
async function load(){
  const s=await (await fetch('/api/state')).json();
  const log=document.getElementById('chat-log');
  log.innerHTML='';
  for(const m of s.messages){
    const d=document.createElement('div');
    d.textContent=(m.role||'')+': '+(m.text||'');
    log.appendChild(d);
  }
}
load();
</script>
</body></html>"""


def self_test():
    steps = []

    def check(name, result, keys):
        if not isinstance(result, dict):
            raise AssertionError("%s: not a dict: %r" % (name, result))
        if "error" in result:
            raise AssertionError("%s: error: %r" % (name, result))
        for k in keys:
            if k not in result:
                raise AssertionError("%s: missing key %s" % (name, k))
        steps.append(name)

    try:
        r = endpoint_reset({"now": "2026-10-19T09:00", "items": [
            {"id": "h1", "kind": "task", "title": "History task", "start": "2026-10-18T20:15", "end": "2026-10-18T20:45", "status": "done"},
            {"id": "c1", "kind": "task", "title": "Clean out the garage", "start": "2026-10-19T10:00", "end": "2026-10-19T11:00"},
        ]})
        check("reset", r, ["now", "items", "messages"])
        if r["now"] != "2026-10-19T09:00":
            raise AssertionError("reset now wrong")
        if len(r["items"]) != 2:
            raise AssertionError("reset items wrong")
        if len(r["messages"]) < 1:
            raise AssertionError("reset no greeting")

        r = endpoint_command({"text": "dentist thursday 3pm", "mode": "command"})
        check("command dentist", r, ["messages", "created"])
        if len(r["messages"]) < 1:
            raise AssertionError("dentist no agent message")

        r = endpoint_command({"text": "lunch with Ana around noon tomorrow", "mode": "command"})
        check("command lunch", r, ["messages", "created"])

        r = endpoint_command({"text": "call mom, portfolio, maybe yoga?", "mode": "dump"})
        check("dump", r, ["messages", "created"])
        if len(r["created"]) != 3:
            raise AssertionError("dump created %d" % len(r["created"]))

        r = endpoint_command({"text": "get my portfolio together", "mode": "command"})
        check("command portfolio", r, ["messages", "created"])
        goal_id = r["created"][0]
        conv = STATE["convo"].get(goal_id)
        if conv != "ask_split":
            raise AssertionError("expected ask_split, got %r" % conv)

        r = endpoint_chat({"item_id": goal_id, "text": "yes"})
        check("chat yes", r, ["messages"])
        if STATE["convo"].get(goal_id) != "ask_why":
            raise AssertionError("expected ask_why")

        r = endpoint_chat({"item_id": goal_id, "text": "to find a new job"})
        check("chat why", r, ["messages"])
        if STATE["convo"].get(goal_id) != "ask_done":
            raise AssertionError("expected ask_done")

        r = endpoint_chat({"item_id": goal_id, "text": "five projects online"})
        check("chat done", r, ["messages"])
        if STATE["convo"].get(goal_id) != "ask_deadline":
            raise AssertionError("expected ask_deadline")

        r = endpoint_chat({"item_id": goal_id, "text": "friday"})
        check("chat deadline", r, ["messages"])
        conv = STATE["convo"].get(goal_id)
        if not isinstance(conv, dict) or conv.get("phase") != "propose":
            raise AssertionError("expected propose")

        r = endpoint_chat({"item_id": goal_id, "text": "looks good"})
        check("chat accept", r, ["messages"])
        if STATE["convo"].get(goal_id) is not None:
            raise AssertionError("expected convo cleared")

        r = endpoint_focus()
        check("focus", r, ["item", "parent", "why", "action"])
        if r["item"] is None:
            raise AssertionError("focus item null")

        fid = r["item"]["id"]
        r = endpoint_items_done(fid)
        check("done", r, ["item"])
        if r["item"]["status"] != "done":
            raise AssertionError("done not set")

        r = endpoint_clock({"now": "2026-10-19T12:00"})
        check("clock noon", r, ["messages"])
        r = endpoint_clock({"now": "2026-10-20T23:59"})
        check("clock day2", r, ["messages"])
        r = endpoint_clock({"now": "2026-10-21T23:59"})
        check("clock day3", r, ["messages"])
        r = endpoint_clock({"now": "2026-10-22T23:59"})
        check("clock day4", r, ["messages"])

        c1 = find_item("c1")
        if c1 is None:
            raise AssertionError("c1 missing")
        if c1["postpone_count"] < 1:
            raise AssertionError("c1 not postponed")
        undo_id = None
        for m in STATE["messages"]:
            if m.get("undo_id") and m.get("item_id") == "c1":
                undo_id = m["undo_id"]
                break
        if undo_id is None:
            raise AssertionError("no undo id for c1")

        r = endpoint_chat({"item_id": "c1", "text": "honestly it feels too big"})
        check("chat c1 too big", r, ["messages"])

        r = endpoint_undo({"undo_id": undo_id})
        check("undo", r, ["ok"])
        c1b = find_item("c1")
        if c1b is None or c1b["postpone_count"] != 1:
            raise AssertionError("undo did not restore")

        r = endpoint_state()
        check("state", r, ["now", "items", "messages"])
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    endpoint_reset({"now": "2026-10-19T09:00", "items": []})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data", type=str, required=True)
    args = ap.parse_args()
    os.makedirs(args.data, exist_ok=True)
    selftest_path = os.path.join(BASE_DIR, "selftest.txt")
    if os.path.exists(selftest_path):
        try:
            with open(selftest_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception:
            content = ""
        if content == "strict":
            self_test()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
