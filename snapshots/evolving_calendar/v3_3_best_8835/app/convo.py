import re
import uuid
import copy
from datetime import datetime, date, time as dtime, timedelta

import v2

ISO = "%Y-%m-%dT%H:%M"

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot", "lazy", "urgent", "alert"]

YES_WORDS = [
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "alright", "please", "looks good",
    "sounds good", "good", "great", "perfect", "fine", "do it", "go ahead", "let's", "works",
    "that works", "why not", "of course", "absolutely", "love it", "nice", "deal", "cool",
    "awesome", "agreed", "go for it", "sounds right", "all good", "add them", "thanks",
    "thank you", "y",
]

NO_WORDS = [
    "no", "nope", "nah", "not now", "not right now", "no thanks", "later", "rather not", "don't",
]

CHANGE_WORDS = [
    "change", "different", "smaller", "shorter", "fewer", "more", "less", "instead", "rather",
    "swap", "replace", "split", "too big", "too long", "too much", "but",
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

VERBS = ["List", "Pick", "Write", "Open", "Collect", "Choose", "Draft", "Email", "Call", "Book",
         "Clear", "Sort", "Find", "Gather", "Send", "Review", "Update", "Upload", "Read"]


# ---------------------------------------------------------------- helpers

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


def now_dt(S):
    return parse_iso(S.get("now")) or datetime(2026, 10, 19, 19, 0)


def kind_text(text):
    if text is None:
        return ""
    out = str(text)
    low = out.lower()
    for bad in BANNED:
        if bad in low:
            out = re.sub(re.escape(bad), "moved", out, flags=re.IGNORECASE)
            low = out.lower()
    return out


def msg(S, kind, text, item_id=None, undo_id=None, steps=None):
    m = {
        "id": "c_" + uuid.uuid4().hex[:10],
        "role": "agent",
        "kind": kind,
        "text": kind_text(text),
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        m["steps"] = steps if steps is not None else []
    S.setdefault("messages", []).append(m)
    return m


def find_item(S, item_id):
    for it in S.get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def get_item(S, item_id):
    it = find_item(S, item_id)
    if it is None:
        raise KeyError(item_id)
    return it


def has_yes(text):
    low = (text or "").lower()
    for w in YES_WORDS:
        if w == "y":
            if re.search(r"\by\b", low):
                return True
        elif re.search(r"\b" + re.escape(w) + r"\b", low):
            return True
    return False


def has_no(text):
    low = (text or "").lower()
    for w in NO_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return True
    return False


def has_change(text):
    low = (text or "").lower()
    for w in CHANGE_WORDS:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return True
    return False


def is_yes(text):
    if has_no(text):
        return False
    return has_yes(text)


def is_no(text):
    return has_no(text)


def one_question(text):
    if text is None:
        return text
    t = str(text)
    first = t.find("?")
    if first == -1:
        return t
    return t[:first + 1] + t[first + 1:].replace("?", "")


def undo_record(S, item):
    uid = "u_" + uuid.uuid4().hex[:10]
    rec = {
        "item_id": item.get("id"),
        "before": copy.deepcopy(item),
        "start": item.get("start"),
        "end": item.get("end"),
        "window": item.get("window"),
        "postpone_count": item.get("postpone_count", 0),
        "used": False,
    }
    S.setdefault("undo", {})[uid] = rec
    return uid


def sent_key(item_id, kind, when):
    return "%s|%s|%s" % (item_id, kind, when)


def already_sent(S, key):
    return key in S.setdefault("sent", {})


def mark_sent(S, key):
    S.setdefault("sent", {})[key] = True


def log(S, kind, item_ids, payload=None, source="clock"):
    try:
        v2.add_log(S, kind, item_ids, payload or {}, source)
    except Exception:
        pass


# ---------------------------------------------------------------- deadline parsing

def parse_deadline(S, text):
    base = now_dt(S)
    today_d = base.date()
    low = (text or "").lower()

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass

    m = re.search(r"\b([a-z]{3,9})\.?\s+(\d{1,2})\b", low)
    if m and m.group(1) in MONTHS:
        try:
            d = date(today_d.year, MONTHS[m.group(1)], int(m.group(2)))
            if d < today_d:
                d = date(today_d.year + 1, MONTHS[m.group(1)], int(m.group(2)))
            return d
        except Exception:
            pass

    if "today" in low:
        return today_d
    if "tomorrow" in low:
        return today_d + timedelta(days=1)

    for name, wd in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            delta = (wd - today_d.weekday()) % 7
            if delta == 0:
                delta = 7
            return today_d + timedelta(days=delta)

    return today_d + timedelta(days=7)


# ---------------------------------------------------------------- breakdown builder

def build_steps(title):
    low = (title or "").lower()
    if "portfolio" in low:
        return [
            {"title": "List the projects you are proudest of", "estimate_min": 10},
            {"title": "Pick the five best projects", "estimate_min": 30},
            {"title": "Write a short note for each project", "estimate_min": 60},
            {"title": "Upload the projects to a simple website", "estimate_min": 60},
        ]
    if "deck" in low or "pitch" in low or "presentation" in low:
        return [
            {"title": "List the points you want to make", "estimate_min": 10},
            {"title": "Pick the three strongest points", "estimate_min": 30},
            {"title": "Draft one slide for each point", "estimate_min": 60},
            {"title": "Review the deck and tidy it up", "estimate_min": 30},
        ]
    if "thesis" in low or "essay" in low or "paper" in low or "write" in low:
        return [
            {"title": "Open a note and list the main ideas", "estimate_min": 10},
            {"title": "Pick the first section to write", "estimate_min": 30},
            {"title": "Draft that section", "estimate_min": 60},
            {"title": "Review it and save it", "estimate_min": 30},
        ]
    if "garage" in low or "closet" in low or "clean" in low or "tidy" in low:
        return [
            {"title": "Clear one small corner first", "estimate_min": 10},
            {"title": "Sort what you find into keep and go", "estimate_min": 30},
            {"title": "Gather the go pile into bags", "estimate_min": 30},
            {"title": "Send or drop off the bags", "estimate_min": 30},
        ]
    if "tax" in low or "application" in low or "claim" in low or "form" in low:
        return [
            {"title": "Gather the papers you need", "estimate_min": 10},
            {"title": "List the questions on the form", "estimate_min": 30},
            {"title": "Draft the answers", "estimate_min": 60},
            {"title": "Review and send it", "estimate_min": 30},
        ]
    return [
        {"title": "Open a note and list what '%s' needs" % (title or "this"), "estimate_min": 10},
        {"title": "Pick the first piece to start", "estimate_min": 20},
        {"title": "Draft the first piece", "estimate_min": 45},
        {"title": "Review it and send or save it", "estimate_min": 30},
    ]


def smaller_steps(title):
    steps = build_steps(title)
    out = []
    for s in steps[:5]:
        est = s.get("estimate_min", 30)
        if est > 30:
            est = 30
        out.append({"title": s["title"], "estimate_min": est})
    if len(out) < 3:
        out.append({"title": "Review it and save it", "estimate_min": 15})
    return out[:5]


def clamp_steps(steps):
    """Enforce contract 4: first step <= 15 min, every step <= 60 min."""
    out = []
    for i, s in enumerate(steps or []):
        try:
            est = int(s.get("estimate_min") or 30)
        except Exception:
            est = 30
        if i == 0:
            if est > 15:
                est = 15
            if est < 5:
                est = 5
        else:
            if est > 60:
                est = 60
            if est < 10:
                est = 10
        out.append({"title": s.get("title"), "estimate_min": est})
    return out


def breakdown_message(S, item, steps=None):
    if steps is None:
        steps = build_steps(item.get("title"))
    steps = clamp_steps(steps)
    text = "Here's a gentle plan for '%s'. Does this look ok?" % (item.get("title") or "this")
    return msg(S, "breakdown", text, item_id=item.get("id"), steps=steps)


# ---------------------------------------------------------------- free slots / usual hour

def usual_hour(S):
    hours = []
    for it in S.get("items", []):
        if it.get("source") == "seed" and it.get("status") == "done":
            st = parse_iso(it.get("start"))
            if st is not None:
                hours.append(st.hour)
    if not hours:
        return 19
    hours.sort()
    n = len(hours)
    if n % 2 == 1:
        return hours[n // 2]
    return hours[(n // 2) - 1]


def overlaps(S, start_dt, end_dt, ignore_id=None):
    for it in S.get("items", []):
        if it.get("status") != "open":
            continue
        if ignore_id is not None and it.get("id") == ignore_id:
            continue
        s = parse_iso(it.get("start"))
        e = parse_iso(it.get("end"))
        if s is None:
            continue
        if e is None:
            e = s + timedelta(minutes=60)
        if s < end_dt and start_dt < e:
            return True
    return False


def find_free_slot(S, day_d, length_min, prefer_hour, ignore_id=None):
    length = timedelta(minutes=length_min)
    h = prefer_hour
    m = 0
    while h < 24:
        start_dt = datetime.combine(day_d, dtime(h, m))
        end_dt = start_dt + length
        if end_dt.date() == day_d or end_dt <= datetime.combine(day_d, dtime(23, 59)):
            if not overlaps(S, start_dt, end_dt, ignore_id):
                return start_dt
        m += 15
        if m >= 60:
            m = 0
            h += 1
    h = 8
    m = 0
    while h < 24:
        start_dt = datetime.combine(day_d, dtime(h, m))
        end_dt = start_dt + length
        if not overlaps(S, start_dt, end_dt, ignore_id):
            return start_dt
        m += 15
        if m >= 60:
            m = 0
            h += 1
    return datetime.combine(day_d, dtime(8, 0))


def schedule_children(S, goal, steps, deadline_d):
    base = now_dt(S)
    hour = usual_hour(S)
    steps = clamp_steps(steps)
    day_d = base.date()
    first_start = datetime.combine(day_d, dtime(hour, 0))
    if first_start <= base:
        day_d = day_d + timedelta(days=1)
    created = []
    for step in steps:
        est = int(step.get("estimate_min") or 30)
        length = est if est > 0 else 30
        placed = None
        guard = 0
        while guard < 60:
            if day_d >= deadline_d:
                break
            slot = find_free_slot(S, day_d, length, hour)
            if slot is not None and slot.date() < deadline_d:
                placed = slot
                break
            day_d = day_d + timedelta(days=1)
            guard += 1
        if placed is None:
            last_d = deadline_d - timedelta(days=1)
            if last_d < base.date():
                last_d = base.date()
            cursor = datetime.combine(last_d, dtime(8, 0))
            for it in S.get("items", []):
                if it.get("status") != "open":
                    continue
                e = parse_iso(it.get("end"))
                if e is not None and e.date() == last_d and e > cursor:
                    cursor = e
            placed = cursor
        end_dt = placed + timedelta(minutes=length)
        child = {
            "id": "i_" + uuid.uuid4().hex[:10],
            "kind": "task",
            "title": step.get("title"),
            "start": fmt_iso(placed),
            "end": fmt_iso(end_dt),
            "window": None,
            "status": "open",
            "why": None,
            "parent_id": goal.get("id"),
            "estimate_min": est,
            "postpone_count": 0,
            "source": "agent",
        }
        S.setdefault("items", []).append(child)
        created.append(child)
        day_d = placed.date() + timedelta(days=1)
    return created


# ---------------------------------------------------------------- reset

def reset(S):
    S["convo"] = {}
    S["sent"] = {}
    S["streak_yes"] = 0
    S["streak_no"] = 0
    if not isinstance(S.get("undo"), dict):
        S["undo"] = {}
    if not isinstance(S.get("messages"), list):
        S["messages"] = []


# ---------------------------------------------------------------- appetite / big task

def on_big_task(S, item):
    if item is None:
        return []
    meta = S.setdefault("meta", {})
    if "breakdown_appetite" not in meta:
        meta["breakdown_appetite"] = 0.5
    appetite = meta.get("breakdown_appetite", 0.5)
    streak_no = S.get("streak_no", 0)
    streak_yes = S.get("streak_yes", 0)
    convo = S.setdefault("convo", {})
    out = []
    if streak_no >= 3 or appetite <= 0.1:
        convo[item.get("id")] = None
        return out
    # Contract 3: exactly one question before any breakdown, whatever the appetite.
    text = "This one sounds big. Want me to break '%s' into small steps?" % (item.get("title") or "this")
    m = msg(S, "question", one_question(text), item_id=item.get("id"))
    convo[item.get("id")] = "ask_split"
    out.append(m)
    return out


# ---------------------------------------------------------------- chat

def chat(S, item_id, text):
    item = get_item(S, item_id)
    convo = S.setdefault("convo", {})
    state = convo.get(item_id)
    text = text or ""

    person = {
        "id": "c_" + uuid.uuid4().hex[:10],
        "role": "person",
        "kind": "reply",
        "text": text,
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": None,
    }
    S.setdefault("messages", []).append(person)

    out = []
    log_kind = "chat"
    if state in ("checkin", "confirm_drop"):
        log_kind = "checkin_answer"

    if state == "ask_split":
        if is_no(text):
            S["streak_no"] = S.get("streak_no", 0) + 1
            S["streak_yes"] = 0
            meta = S.setdefault("meta", {})
            app = meta.get("breakdown_appetite", 0.5)
            meta["breakdown_appetite"] = round(max(0.0, app - 0.15), 2)
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep it as one task.", item_id=item_id))
        elif is_yes(text):
            S["streak_yes"] = S.get("streak_yes", 0) + 1
            S["streak_no"] = 0
            meta = S.setdefault("meta", {})
            app = meta.get("breakdown_appetite", 0.5)
            meta["breakdown_appetite"] = round(min(1.0, app + 0.15), 2)
            convo[item_id] = "ask_why"
            out.append(msg(S, "question", "Why does this matter to you?", item_id=item_id))
        else:
            out.append(msg(S, "question",
                           "No pressure. Shall I break it into small steps, yes or not now?",
                           item_id=item_id))

    elif state == "ask_why":
        item["why"] = text
        convo[item_id] = "ask_done"
        out.append(msg(S, "question", "What does done look like for you?", item_id=item_id))

    elif state == "ask_done":
        convo[item_id] = "ask_deadline"
        out.append(msg(S, "question", "Is there a deadline, a day you'd like it finished by?",
                       item_id=item_id))

    elif state == "ask_deadline":
        deadline_d = parse_deadline(S, text)
        item["deadline"] = deadline_d.strftime("%Y-%m-%d")
        convo[item_id] = "propose"
        out.append(breakdown_message(S, item))

    elif state == "propose":
        if is_no(text):
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep it as one task.", item_id=item_id))
        elif has_change(text):
            out.append(breakdown_message(S, item, smaller_steps(item.get("title"))))
        elif is_yes(text):
            steps = build_steps(item.get("title"))
            deadline_d = None
            stored = item.get("deadline")
            if stored:
                try:
                    deadline_d = datetime.strptime(stored, "%Y-%m-%d").date()
                except Exception:
                    deadline_d = None
            if deadline_d is None:
                deadline_d = now_dt(S).date() + timedelta(days=7)
            created = schedule_children(S, item, steps, deadline_d)
            convo[item_id] = None
            first = steps[0]["title"] if steps else "the first step"
            out.append(msg(S, "info", "Done, the first tiny step is '%s'." % first, item_id=item_id))
            for c in created:
                log(S, "create", [c.get("id")], {"title": c.get("title")}, "agent")
        else:
            out.append(msg(S, "question", "Shall I add these steps to your calendar?",
                           item_id=item_id))

    elif state == "checkin":
        low = text.lower()
        if "big" in low or "smaller" in low or "break" in low or "too much" in low:
            convo[item_id] = "propose"
            out.append(breakdown_message(S, item))
        elif "time" in low or "busy" in low or "wrong" in low:
            old_start = parse_iso(item.get("start"))
            old_end = parse_iso(item.get("end"))
            length = 60
            if old_start is not None and old_end is not None:
                length = max(15, int((old_end - old_start).total_seconds() // 60))
            base = now_dt(S)
            day_d = base.date() + timedelta(days=1)
            slot = find_free_slot(S, day_d, length, usual_hour(S), ignore_id=item_id)
            uid = undo_record(S, item)
            item["start"] = fmt_iso(slot)
            item["end"] = fmt_iso(slot + timedelta(minutes=length))
            convo[item_id] = None
            out.append(msg(S, "info",
                           "Okay, I moved '%s' to %s." % (item.get("title"), slot.strftime("%a %b %d at %H:%M")),
                           item_id=item_id, undo_id=uid))
            log(S, "update", [item_id], {"start": item.get("start")}, "person")
        elif "not needed" in low or "drop" in low or "remove" in low or "don't need" in low:
            convo[item_id] = "confirm_drop"
            out.append(msg(S, "question", "Shall I remove '%s' for you?" % (item.get("title") or "this"),
                           item_id=item_id))
        else:
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep '%s'." % (item.get("title") or "this"),
                           item_id=item_id))

    elif state == "confirm_drop":
        if is_yes(text):
            items = S.get("items", [])
            idx = None
            for i, it in enumerate(items):
                if it.get("id") == item_id:
                    idx = i
                    break
            if idx is not None:
                uid = undo_record(S, items[idx])
                S["undo"][uid]["index"] = idx
                del items[idx]
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I removed '%s'." % (item.get("title") or "this"),
                           item_id=item_id))
            log(S, "delete", [item_id], {}, "person")
        else:
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep '%s'." % (item.get("title") or "this"),
                           item_id=item_id))

    else:
        out.append(msg(S, "info", "Okay, I'm here whenever you want to plan a little.",
                       item_id=item_id))

    log(S, log_kind, [item_id], {"text": text}, "person")
    try:
        v2.tick(S, False)
    except Exception:
        pass
    return {"messages": out}


# ---------------------------------------------------------------- clock / scheduler

def due_moments(S, old_dt, new_dt):
    moments = set()
    for it in S.get("items", []):
        if it.get("status") != "open":
            continue
        st = parse_iso(it.get("start"))
        en = parse_iso(it.get("end"))
        if st is not None:
            if old_dt < st <= new_dt:
                moments.add(st)
            if en is None:
                en = st + timedelta(minutes=60)
        if en is not None:
            before = en - timedelta(minutes=10)
            if old_dt < before <= new_dt:
                moments.add(before)
            catch = en + timedelta(minutes=31)
            if catch <= new_dt:
                moments.add(catch)
    return sorted(moments)


def has_children(S, item_id):
    for it in S.get("items", []):
        if it.get("parent_id") == item_id and it.get("status") == "open":
            return True
    return False


def clock(S, new_iso):
    old_dt = now_dt(S)
    new_dt = parse_iso(new_iso)
    if new_dt is None:
        return {"messages": []}
    if new_dt <= old_dt:
        return {"messages": []}

    out = []
    checkin_done = False
    moments = due_moments(S, old_dt, new_dt)

    for t in moments:
        for it in list(S.get("items", [])):
            if it.get("status") != "open":
                continue
            iid = it.get("id")
            st = parse_iso(it.get("start"))
            en = parse_iso(it.get("end"))
            if st is not None and st == t:
                key = sent_key(iid, "start", it.get("start"))
                if not already_sent(S, key):
                    mark_sent(S, key)
                    m = msg(S, "reminder",
                            "It's time for '%s' (%s)." % (it.get("title"), st.strftime("%H:%M")),
                            item_id=iid)
                    out.append(m)
                    log(S, "reminder", [iid], {"start": it.get("start")}, "clock")
            if en is not None:
                before = en - timedelta(minutes=10)
                if before == t:
                    key = sent_key(iid, "end", it.get("end"))
                    if not already_sent(S, key):
                        mark_sent(S, key)
                        m = msg(S, "reminder",
                                "'%s' wraps up in 10 minutes." % (it.get("title") or "this"),
                                item_id=iid)
                        out.append(m)
                        log(S, "reminder", [iid], {"end": it.get("end")}, "clock")
                catch = en + timedelta(minutes=31)
                if catch <= t:
                    if it.get("kind") != "task":
                        continue
                    if has_children(S, iid):
                        continue
                    if st is None:
                        continue
                    if it.get("postpone_count", 0) >= 2:
                        key = sent_key(iid, "checkin", it.get("end"))
                        if already_sent(S, key):
                            continue
                        if checkin_done:
                            continue
                        mark_sent(S, key)
                        checkin_done = True
                        S.setdefault("convo", {})[iid] = "checkin"
                        m = msg(S, "checkin",
                                "'%s' has moved a few times, and that's okay. Shall I break it into smaller steps, find a better time, or let it go?" % (it.get("title") or "this"),
                                item_id=iid)
                        out.append(m)
                        log(S, "checkin_answer", [iid], {"end": it.get("end")}, "clock")
                    else:
                        length = 60
                        if en is not None and st is not None:
                            length = max(15, int((en - st).total_seconds() // 60))
                        end_d = en.date()
                        new_day_d = end_d + timedelta(days=1)
                        if new_day_d < t.date():
                            new_day_d = t.date()
                        slot = find_free_slot(S, new_day_d, length, usual_hour(S), ignore_id=iid)
                        if slot <= t:
                            slot = find_free_slot(S, t.date() + timedelta(days=1), length,
                                                  usual_hour(S), ignore_id=iid)
                        uid = undo_record(S, it)
                        it["start"] = fmt_iso(slot)
                        it["end"] = fmt_iso(slot + timedelta(minutes=length))
                        it["postpone_count"] = it.get("postpone_count", 0) + 1
                        m = msg(S, "postponed",
                                "No worries, I moved '%s' to %s. Undo if you'd rather keep it." % (
                                    it.get("title") or "this",
                                    slot.strftime("%a %b %d at %H:%M")),
                                item_id=iid, undo_id=uid)
                        out.append(m)
                        log(S, "postponed", [iid], {"start": it.get("start")}, "clock")

    S["now"] = new_iso
    try:
        v2.tick(S, True)
    except Exception:
        pass
    return {"messages": out}
