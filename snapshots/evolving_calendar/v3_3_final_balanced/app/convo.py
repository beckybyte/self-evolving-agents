import copy
import re
import uuid
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

VERBS = ["List", "Pick", "Write", "Open", "Collect", "Choose", "Draft", "Email", "Call", "Book",
         "Clear", "Sort", "Find", "Gather", "Send", "Review", "Update", "Upload", "Read"]

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


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
    return parse_iso(S.get("now")) or datetime(2026, 10, 19, 20, 0)


def kind_text(text):
    if text is None:
        return ""
    out = text
    for bad in BANNED:
        if bad in out.lower():
            out = re.sub(re.escape(bad), "gently", out, flags=re.IGNORECASE)
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
        m["steps"] = steps or []
    S["messages"].append(m)
    return m


def person_msg(S, text, item_id=None):
    m = {
        "id": "c_" + uuid.uuid4().hex[:10],
        "role": "person",
        "kind": "reply",
        "text": text,
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": None,
    }
    S["messages"].append(m)
    return m


def find_item(S, item_id):
    for it in S.get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def has_yes(text):
    t = (text or "").lower()
    for w in YES_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t):
            return True
    return False


def has_no(text):
    t = (text or "").lower()
    for w in NO_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t):
            return True
    return False


def has_change(text):
    t = (text or "").lower()
    for w in CHANGE_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t):
            return True
    return False


def undo_record(S, item):
    uid = "u_" + uuid.uuid4().hex[:10]
    S.setdefault("undo", {})[uid] = {
        "item_id": item.get("id"),
        "before": copy.deepcopy(item),
        "start": item.get("start"),
        "end": item.get("end"),
        "window": item.get("window"),
        "postpone_count": item.get("postpone_count", 0),
    }
    return uid


def sent_key(item_id, kind, when):
    return "%s|%s|%s" % (item_id, kind, when)


def is_sent(S, key):
    return key in S.setdefault("sent", {})


def mark_sent(S, key):
    S.setdefault("sent", {})[key] = True


# ---------------------------------------------------------------- deadline parsing

def parse_deadline(S, text):
    base = now_dt(S)
    t = (text or "").lower()
    if not t.strip():
        return base + timedelta(days=7)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 23, 59)
        except Exception:
            pass
    m = re.search(r"([a-z]+)\.?\s+(\d{1,2})", t)
    if m and m.group(1) in MONTHS:
        try:
            day_d = date(base.year, MONTHS[m.group(1)], int(m.group(2)))
            if day_d < base.date():
                day_d = date(base.year + 1, MONTHS[m.group(1)], int(m.group(2)))
            return datetime.combine(day_d, dtime(23, 59))
        except Exception:
            pass
    if "today" in t:
        return datetime.combine(base.date(), dtime(23, 59))
    if "tomorrow" in t:
        return datetime.combine(base.date() + timedelta(days=1), dtime(23, 59))
    for name, wd in WEEKDAYS.items():
        if re.search(r"(?<![a-z])" + name + r"(?![a-z])", t):
            delta = (wd - base.weekday()) % 7
            if delta == 0:
                delta = 7
            return datetime.combine(base.date() + timedelta(days=delta), dtime(23, 59))
    return base + timedelta(days=7)


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
    return [
        {"title": "Open a note and list what '%s' needs" % title, "estimate_min": 10},
        {"title": "Pick the first piece to start", "estimate_min": 20},
        {"title": "Draft the first piece", "estimate_min": 45},
        {"title": "Review it and send or save it", "estimate_min": 30},
    ]


def smaller_steps(title):
    steps = build_steps(title)
    out = []
    for s in steps[:5]:
        est = max(5, min(30, int(s["estimate_min"])))
        out.append({"title": s["title"], "estimate_min": est})
    return out


def breakdown_message(S, item, steps=None):
    steps = steps or build_steps(item.get("title", ""))
    text = "Here's a gentle plan for '%s'. Does this look ok?" % item.get("title", "")
    return msg(S, "breakdown", text, item_id=item.get("id"), steps=steps)


# ---------------------------------------------------------------- free slot finder

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


def free_slot(S, day_d, length_min, usual_hour, after_dt=None):
    length = timedelta(minutes=length_min)
    start_h = usual_hour
    for h in range(start_h, 24):
        for m in (0, 15, 30, 45):
            if h == 23 and m > 30:
                continue
            cand = datetime.combine(day_d, dtime(h, m))
            if after_dt is not None and cand <= after_dt:
                continue
            if not overlaps(S, cand, cand + length):
                return cand
    for h in range(8, 24):
        for m in (0, 15, 30, 45):
            if h == 23 and m > 30:
                continue
            cand = datetime.combine(day_d, dtime(h, m))
            if after_dt is not None and cand <= after_dt:
                continue
            if not overlaps(S, cand, cand + length):
                return cand
    return datetime.combine(day_d, dtime(usual_hour, 0))


def usual_hour(S):
    hours = []
    for it in S.get("items", []):
        if it.get("source") == "seed" and it.get("status") == "done":
            s = parse_iso(it.get("start"))
            if s is not None:
                hours.append(s.hour)
    if not hours:
        return 19
    hours.sort()
    return hours[(len(hours) - 1) // 2]


# ---------------------------------------------------------------- reset

def reset(S):
    S["convo"] = {}
    S["sent"] = {}
    S["streak_yes"] = 0
    S["streak_no"] = 0
    if "meta" not in S or not isinstance(S.get("meta"), dict):
        S["meta"] = {}
    S["meta"].setdefault("breakdown_appetite", 0.5)
    S["meta"].setdefault("threshold", 0.7)
    S["meta"].setdefault("novelty", 0.5)
    S["meta"].setdefault("cadence_hours", 24)
    S["meta"].setdefault("tone", "gentle")


# ---------------------------------------------------------------- appetite / big task

def on_big_task(S, item):
    if item is None:
        return []
    meta = S.setdefault("meta", {})
    appetite = float(meta.get("breakdown_appetite", 0.5))
    streak_no = int(S.get("streak_no", 0))
    streak_yes = int(S.get("streak_yes", 0))
    convo = S.setdefault("convo", {})
    if streak_no >= 3 or appetite <= 0.1:
        convo[item["id"]] = None
        return []
    if streak_yes >= 3 or appetite >= 0.9:
        m = breakdown_message(S, item)
        convo[item["id"]] = "propose"
        return [m]
    text = "This one sounds big. Want me to break '%s' into small steps?" % item.get("title", "")
    m = msg(S, "question", text, item_id=item["id"])
    convo[item["id"]] = "ask_split"
    return [m]


# ---------------------------------------------------------------- accept breakdown

def accept_breakdown(S, item, steps):
    base = now_dt(S)
    deadline = parse_deadline(S, item.get("why") or "")
    deadline_d = deadline.date()
    uh = usual_hour(S)
    created = []
    day_d = base.date()
    for idx, step in enumerate(steps):
        est = int(step.get("estimate_min") or 30)
        placed = None
        tries = 0
        while tries < 60:
            if day_d >= deadline_d:
                break
            cand = free_slot(S, day_d, est, uh)
            if cand.date() < deadline_d:
                placed = cand
                break
            day_d = day_d + timedelta(days=1)
            tries += 1
        if placed is None:
            break
        child = {
            "id": "i_" + uuid.uuid4().hex[:10],
            "kind": "task",
            "title": step.get("title"),
            "start": fmt_iso(placed),
            "end": fmt_iso(placed + timedelta(minutes=est)),
            "window": None,
            "status": "open",
            "why": None,
            "parent_id": item.get("id"),
            "estimate_min": est,
            "postpone_count": 0,
            "source": "agent",
        }
        S["items"].append(child)
        created.append(child)
        day_d = placed.date() + timedelta(days=1)
    if not created:
        # place back to back on the last day before the deadline
        last_d = deadline_d - timedelta(days=1)
        if last_d < base.date():
            last_d = base.date()
        cursor = datetime.combine(last_d, dtime(uh, 0))
        for step in steps:
            est = int(step.get("estimate_min") or 30)
            child = {
                "id": "i_" + uuid.uuid4().hex[:10],
                "kind": "task",
                "title": step.get("title"),
                "start": fmt_iso(cursor),
                "end": fmt_iso(cursor + timedelta(minutes=est)),
                "window": None,
                "status": "open",
                "why": None,
                "parent_id": item.get("id"),
                "estimate_min": est,
                "postpone_count": 0,
                "source": "agent",
            }
            S["items"].append(child)
            created.append(child)
            cursor = cursor + timedelta(minutes=est)
    return created


# ---------------------------------------------------------------- chat

def chat(S, item_id, text):
    item = find_item(S, item_id)
    if item is None:
        raise KeyError(item_id)
    convo = S.setdefault("convo", {})
    state = convo.get(item_id)
    out = []
    person_msg(S, text, item_id)
    log_kind = "chat"
    if state in ("checkin", "confirm_drop"):
        log_kind = "checkin_answer"

    if state == "ask_split":
        if has_no(text):
            S["streak_no"] = int(S.get("streak_no", 0)) + 1
            S["streak_yes"] = 0
            meta = S.setdefault("meta", {})
            meta["breakdown_appetite"] = round(max(0.0, float(meta.get("breakdown_appetite", 0.5)) - 0.15), 2)
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep it as one task.", item_id=item_id))
        elif has_yes(text):
            S["streak_yes"] = int(S.get("streak_yes", 0)) + 1
            S["streak_no"] = 0
            meta = S.setdefault("meta", {})
            meta["breakdown_appetite"] = round(min(1.0, float(meta.get("breakdown_appetite", 0.5)) + 0.15), 2)
            convo[item_id] = "ask_why"
            out.append(msg(S, "question", "Why does this matter to you?", item_id=item_id))
        else:
            out.append(msg(S, "question", "No pressure. Shall I break it into small steps, yes or not now?", item_id=item_id))

    elif state == "ask_why":
        item["why"] = text
        convo[item_id] = "ask_done"
        out.append(msg(S, "question", "What does done look like for you?", item_id=item_id))

    elif state == "ask_done":
        convo[item_id] = "ask_deadline"
        out.append(msg(S, "question", "Is there a deadline, a day you'd like it finished by?", item_id=item_id))

    elif state == "ask_deadline":
        deadline = parse_deadline(S, text)
        item["_deadline"] = fmt_iso(deadline)
        convo[item_id] = "propose"
        out.append(breakdown_message(S, item))

    elif state == "propose":
        if has_change(text) and not (has_yes(text) and not has_change(text)):
            out.append(breakdown_message(S, item, smaller_steps(item.get("title", ""))))
        elif has_no(text):
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep it as one task.", item_id=item_id))
        elif has_yes(text):
            steps = build_steps(item.get("title", ""))
            created = accept_breakdown(S, item, steps)
            convo[item_id] = None
            first = steps[0]["title"] if steps else "the first step"
            out.append(msg(S, "info", "Done, the first tiny step is '%s'." % first, item_id=item_id))
            for c in created:
                try:
                    v2.add_log(S, "create", [c["id"]], {"title": c.get("title")}, "agent")
                except Exception:
                    pass
        else:
            out.append(msg(S, "question", "Shall I add these steps to your calendar?", item_id=item_id))

    elif state == "checkin":
        low = (text or "").lower()
        if "big" in low or "smaller" in low or "break" in low or "too much" in low:
            convo[item_id] = "propose"
            out.append(breakdown_message(S, item))
        elif "time" in low or "busy" in low or "wrong" in low:
            moved = move_to_next_day(S, item)
            if moved:
                out.append(moved)
            else:
                out.append(msg(S, "info", "Okay, I'll keep '%s' where it is." % item.get("title", ""), item_id=item_id))
        elif "not needed" in low or "drop" in low or "remove" in low or "don't need" in low:
            convo[item_id] = "confirm_drop"
            out.append(msg(S, "question", "Shall I remove '%s' for you?" % item.get("title", ""), item_id=item_id))
        else:
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep '%s'." % item.get("title", ""), item_id=item_id))

    elif state == "confirm_drop":
        if has_yes(text):
            convo[item_id] = None
            try:
                S["items"].remove(item)
            except ValueError:
                pass
            try:
                v2.add_log(S, "delete", [item_id], {"title": item.get("title")}, "person")
            except Exception:
                pass
            out.append(msg(S, "info", "Okay, I've removed '%s'." % item.get("title", ""), item_id=item_id))
        else:
            convo[item_id] = None
            out.append(msg(S, "info", "Okay, I'll keep '%s'." % item.get("title", ""), item_id=item_id))

    else:
        out.append(msg(S, "info", "Okay, I'm here whenever you want to plan a little.", item_id=item_id))

    try:
        v2.add_log(S, log_kind, [item_id], {"text": text}, "person")
    except Exception:
        pass
    try:
        v2.tick(S, False)
    except Exception:
        pass
    return {"messages": out}


def move_to_next_day(S, item):
    start = parse_iso(item.get("start"))
    end = parse_iso(item.get("end"))
    base = now_dt(S)
    if start is None:
        start = base
    length = 60
    if end is not None and end > start:
        length = int((end - start).total_seconds() // 60)
    uh = usual_hour(S)
    day_d = start.date() + timedelta(days=1)
    if day_d < base.date():
        day_d = base.date()
    cand = free_slot(S, day_d, length, uh, after_dt=base)
    uid = undo_record(S, item)
    item["start"] = fmt_iso(cand)
    item["end"] = fmt_iso(cand + timedelta(minutes=length))
    try:
        v2.add_log(S, "update", [item.get("id")], {"start": item["start"]}, "person")
    except Exception:
        pass
    return msg(S, "postponed",
               "No worries, I moved '%s' to %s. Undo if you'd rather keep it." % (
                   item.get("title", ""), cand.strftime("%a %b %d at %H:%M")),
               item_id=item.get("id"), undo_id=uid)


# ---------------------------------------------------------------- clock

def clock(S, new_iso):
    new_dt = parse_iso(new_iso)
    old_dt = now_dt(S)
    if new_dt is None or new_dt <= old_dt:
        return {"messages": []}
    out = []
    items = list(S.get("items", []))
    moments = set()
    for it in items:
        if it.get("status") != "open":
            continue
        s = parse_iso(it.get("start"))
        e = parse_iso(it.get("end"))
        if s is not None:
            moments.add(s)
        if e is not None:
            moments.add(e - timedelta(minutes=10))
            moments.add(e + timedelta(minutes=31))
        elif s is not None:
            moments.add(s + timedelta(minutes=60) + timedelta(minutes=31))
    due = sorted([m for m in moments if old_dt < m <= new_dt])
    checkin_done = False
    for t in due:
        for it in items:
            if it.get("status") != "open":
                continue
            s = parse_iso(it.get("start"))
            e = parse_iso(it.get("end"))
            if s is not None and s == t:
                key = sent_key(it.get("id"), "start", it.get("start"))
                if not is_sent(S, key):
                    mark_sent(S, key)
                    m = msg(S, "reminder", "It's time for '%s' (%s)." % (
                        it.get("title", ""), s.strftime("%H:%M")), item_id=it.get("id"))
                    out.append(m)
                    try:
                        v2.add_log(S, "reminder", [it.get("id")], {}, "clock")
                    except Exception:
                        pass
            if e is not None and e - timedelta(minutes=10) == t:
                key = sent_key(it.get("id"), "end", it.get("end"))
                if not is_sent(S, key):
                    mark_sent(S, key)
                    m = msg(S, "reminder", "'%s' wraps up in 10 minutes." % it.get("title", ""),
                            item_id=it.get("id"))
                    out.append(m)
                    try:
                        v2.add_log(S, "reminder", [it.get("id")], {}, "clock")
                    except Exception:
                        pass
            if it.get("kind") != "task":
                continue
            if it.get("parent_id") is not None:
                continue
            if s is None:
                continue
            eff_end = e if e is not None else s + timedelta(minutes=60)
            if t >= eff_end + timedelta(minutes=31):
                if int(it.get("postpone_count", 0)) >= 2:
                    key = sent_key(it.get("id"), "checkin", fmt_iso(eff_end))
                    if is_sent(S, key) or checkin_done:
                        continue
                    mark_sent(S, key)
                    checkin_done = True
                    S.setdefault("convo", {})[it.get("id")] = "checkin"
                    m = msg(S, "checkin",
                            "'%s' has moved a few times, and that's okay. Shall I break it into smaller steps, find a better time, or let it go?" % it.get("title", ""),
                            item_id=it.get("id"))
                    out.append(m)
                else:
                    key = sent_key(it.get("id"), "postpone", fmt_iso(eff_end))
                    if is_sent(S, key):
                        continue
                    mark_sent(S, key)
                    length = int((eff_end - s).total_seconds() // 60)
                    if length <= 0:
                        length = 60
                    uh = usual_hour(S)
                    day_d = eff_end.date() + timedelta(days=1)
                    if day_d < t.date():
                        day_d = t.date()
                    cand = free_slot(S, day_d, length, uh, after_dt=t)
                    uid = undo_record(S, it)
                    it["start"] = fmt_iso(cand)
                    it["end"] = fmt_iso(cand + timedelta(minutes=length))
                    it["postpone_count"] = int(it.get("postpone_count", 0)) + 1
                    m = msg(S, "postponed",
                            "No worries, I moved '%s' to %s. Undo if you'd rather keep it." % (
                                it.get("title", ""), cand.strftime("%a %b %d at %H:%M")),
                            item_id=it.get("id"), undo_id=uid)
                    out.append(m)
                    try:
                        v2.add_log(S, "postponed", [it.get("id")], {"start": it["start"]}, "clock")
                    except Exception:
                        pass
    S["now"] = new_iso
    try:
        v2.tick(S, True)
    except Exception:
        pass
    return {"messages": out}
