import json
import re
import uuid
import hashlib
from datetime import datetime, date, time as dtime, timedelta

import v3

# === SECTION: constants ===

STOP_WORDS = {
    "move", "reschedule", "push", "shift", "postpone", "bump", "delay", "put",
    "off", "back", "by", "later", "earlier", "cancel", "delete", "remove",
    "drop", "scrap", "skip", "my", "the", "a", "an", "to", "on", "at", "for",
    "next", "week", "weeks", "day", "days", "one", "please", "it",
}

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
    "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4,
    "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

CLEAR_PHRASES = [
    "empty", "clear out", "clear", "free up", "wipe", "nothing on", "no plans",
    "keep", "block out", "cancel everything", "cancel all", "make room",
]

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot",
          "lazy", "urgent", "alert"]

AGENT_CORES = {
    "designer": "You design calm calendar views that never hide an item without counting it.",
    "followthrough": "You help the person take one tiny step at a time, without guilt.",
    "learner": "You learn gentle habits from what already happened and never scold.",
}


# === SECTION: helpers ===

def _now_dt(S):
    try:
        return datetime.strptime(S["now"], "%Y-%m-%dT%H:%M")
    except Exception:
        return datetime(2026, 10, 19, 9, 0)


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except Exception:
        return None


def _fmt_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def _new_id(prefix):
    return prefix + uuid.uuid4().hex[:10]


def _clean_text(t):
    low = t.lower()
    for b in BANNED:
        if b in low:
            low = low.replace(b, "gentle")
    return low


def _msg(S, kind, text, item_id=None, undo_id=None, steps=None, role="agent"):
    m = {
        "id": _new_id("msg_"),
        "role": role,
        "kind": kind,
        "text": _clean_text(text),
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        m["steps"] = steps or []
    S["messages"].append(m)
    return m


def _info(S, text, item_id=None):
    return _msg(S, "info", text, item_id=item_id)


def _reply(S, text, item_id=None):
    return _msg(S, "reply", text, item_id=item_id)


def _copy_item(it):
    return json.loads(json.dumps(it))


def _default_item():
    return {
        "id": None, "kind": "task", "title": "", "start": None, "end": None,
        "window": None, "status": "open", "why": None, "parent_id": None,
        "estimate_min": None, "postpone_count": 0, "source": "seed",
    }


def _find_item(S, iid):
    for it in S["items"]:
        if it.get("id") == iid:
            return it
    return None


def _open_items(S):
    return [it for it in S["items"] if it.get("status") == "open"]


def _words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def _match_words(text):
    return set(w for w in _words(text) if w not in STOP_WORDS)


def _best_match(S, text):
    mw = _match_words(text)
    best = None
    best_score = 0
    for it in _open_items(S):
        title_words = [w for w in _words(it.get("title") or "") if len(w) >= 3]
        score = sum(1 for w in title_words if w in mw)
        if score > best_score:
            best_score = score
            best = it
        elif score == best_score and score > 0 and best is not None:
            a = _parse_iso(it.get("start"))
            b = _parse_iso(best.get("start"))
            if a is not None and (b is None or a < b):
                best = it
    return best if best_score > 0 else None


def _day_word_date(S, text):
    """Return a date for a day word in text, or None."""
    now = _now_dt(S)
    today = now.date()
    low = text.lower()
    if re.search(r"\btoday\b", low):
        return today
    if re.search(r"\btomorrow\b", low):
        return today + timedelta(days=1)
    for name, idx in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            delta = (idx - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    m = re.search(r"\b([a-z]+)\s+(\d{1,2})\b", low)
    if m and m.group(1) in MONTHS:
        try:
            return date(today.year, MONTHS[m.group(1)], int(m.group(2)))
        except Exception:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", low)
    if m:
        d = int(m.group(1))
        try:
            cand = date(today.year, today.month, d)
            if cand < today:
                nm = today.month + 1
                ny = today.year
                if nm > 12:
                    nm = 1
                    ny += 1
                cand = date(ny, nm, d)
            return cand
        except Exception:
            pass
    return None


def _part_of_day(text):
    low = text.lower()
    if re.search(r"\bmorning\b", low):
        return (0, 12)
    if re.search(r"\bafternoon\b", low):
        return (12, 17)
    if re.search(r"\bevening\b", low) or re.search(r"\btonight\b", low):
        return (17, 24)
    return None


def _range_for(S, text):
    """Return (start_dt, end_dt, label) for a bulk range, or None."""
    now = _now_dt(S)
    today = now.date()
    low = text.lower()
    pod = _part_of_day(text)
    if re.search(r"\bweekend\b", low):
        wd = today.weekday()
        if wd == 5:
            sat = today
        elif wd == 6:
            sat = today - timedelta(days=1)
        else:
            sat = today + timedelta(days=(5 - wd) % 7)
        start = datetime.combine(sat, dtime(0, 0))
        end = datetime.combine(sat + timedelta(days=2), dtime(0, 0))
        return (start, end, "weekend")
    d = _day_word_date(S, text)
    if d is not None:
        if pod:
            start = datetime.combine(d, dtime(pod[0], 0))
            end = datetime.combine(d, dtime(pod[1] % 24, 0)) if pod[1] < 24 else datetime.combine(d + timedelta(days=1), dtime(0, 0))
            return (start, end, text.strip())
        start = datetime.combine(d, dtime(0, 0))
        end = datetime.combine(d + timedelta(days=1), dtime(0, 0))
        return (start, end, text.strip())
    return None


def _overlaps(it, start, end):
    s = _parse_iso(it.get("start"))
    if s is None:
        return False
    e = _parse_iso(it.get("end"))
    if e is None:
        e = s + timedelta(minutes=60)
    return s < end and e > start


def _purpose_from(text):
    low = text.lower()
    m = re.search(r"\bfor\s+(.+)$", low)
    if m:
        p = m.group(1)
    else:
        m = re.search(r"[,;]\s*(.+)$", low)
        if m:
            p = m.group(1)
        else:
            m = re.search(r"\b(?:because|since|as)\s+(.+)$", low)
            p = m.group(1) if m else ""
    p = p.strip().rstrip(".!?")
    for lead in ["i have", "i've got", "i got", "i'm", "i am", "we have", "we're",
                 "there's", "a", "an", "my", "some", "please"]:
        if p.startswith(lead + " "):
            p = p[len(lead) + 1:]
        elif p == lead:
            p = ""
    p = p.strip()
    if not p:
        return "Time off"
    return p[0].upper() + p[1:]


def _add_log(S, kind, item_ids, payload, source):
    entry = {
        "id": _new_id("log_"),
        "at": S.get("now"),
        "kind": kind,
        "item_ids": list(item_ids or []),
        "payload": payload or {},
        "source": source,
    }
    S["log"].append(entry)
    return entry


def _activity(S, agent, kind, text, extra=None):
    ev = {"at": S.get("now"), "agent": agent, "kind": kind, "text": _clean_text(text)}
    if extra:
        ev.update(extra)
    S["activity"].append(ev)
    return ev


# === SECTION: init ===

def init_v2(S):
    S["log"] = []
    S["beliefs"] = {}
    S["deleted_beliefs"] = []
    S["penalty"] = {}
    S["blocked"] = {}
    S["proposals"] = []
    S["plans"] = {}
    S["view"] = {"type": "week", "params": {}}
    S["meta"] = {
        "novelty": 0.6,
        "cadence_hours": 24,
        "breakdown_appetite": 0.5,
        "tone": "gentle",
        "threshold": 0.7,
    }
    S["invented_reverts"] = []
    S["last_proposal_at"] = None
    S["learn_mark"] = 0
    S["activity"] = []
    S["dismiss_streak"] = 0
    prompts = []
    for agent, core in AGENT_CORES.items():
        prompts.append({
            "agent": agent,
            "version": 1,
            "core_hash": hashlib.sha256(core.encode("utf-8")).hexdigest()[:12],
            "learned": "",
            "diff": "",
            "reason": "initial version",
            "evidence": [],
            "replay": {"passed": True, "detail": "initial"},
            "active": True,
        })
    S["prompts"] = prompts


def add_log(S, kind, item_ids, payload, source):
    return _add_log(S, kind, item_ids, payload, source)


# === SECTION: command handling ===

def handle_command(S, text):
    if not text:
        return None
    low = text.lower().strip()
    r = _try_bulk(S, low, text)
    if r is not None:
        return r
    r = _try_query(S, low, text)
    if r is not None:
        return r
    r = _try_cancel(S, low, text)
    if r is not None:
        return r
    r = _try_move(S, low, text)
    if r is not None:
        return r
    return None


def _try_bulk(S, low, text):
    has_clear = any(p in low for p in CLEAR_PHRASES)
    if not has_clear:
        return None
    rng = _range_for(S, text)
    if rng is None:
        return None
    start, end, label = rng
    affected = []
    for it in _open_items(S):
        if _overlaps(it, start, end):
            affected.append(it)
    purpose = _purpose_from(text)
    if re.search(r"\bweekend\b", low):
        new_start = datetime.combine(start.date() + timedelta(days=1), dtime(9, 0))
        new_end = datetime.combine(start.date() + timedelta(days=2), dtime(18, 0))
        win = "weekend"
    else:
        pod = _part_of_day(text)
        d = start.date()
        if pod:
            new_start = datetime.combine(d, dtime(max(pod[0], 9), 0))
            new_end = datetime.combine(d, dtime(min(pod[1], 23), 0))
            win = label
        else:
            new_start = datetime.combine(d, dtime(9, 0))
            new_end = datetime.combine(d, dtime(18, 0))
            win = label
    new_item = _default_item()
    new_item["id"] = _new_id("itm_")
    new_item["kind"] = "event"
    new_item["title"] = purpose
    new_item["start"] = _fmt_iso(new_start)
    new_item["end"] = _fmt_iso(new_end)
    new_item["window"] = win
    new_item["source"] = "typed"
    ops = []
    for it in affected:
        ops.append({"op": "delete", "item_id": it["id"], "before": _copy_item(it), "after": None})
    ops.append({"op": "create", "item_id": None, "before": None, "after": new_item})
    plan = {
        "id": _new_id("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": ops,
        "affected": [it["id"] for it in affected],
    }
    S["plans"][plan["id"]] = plan
    n = len(affected)
    msg = _info(S, "I can clear %d item%s and add '%s'. Shall I apply it?" % (
        n, "" if n == 1 else "s", purpose))
    return {"messages": [msg], "created": [], "plan": plan}


def _try_query(S, low, text):
    starts = ("what", "what's", "whats", "anything", "any", "show", "when",
              "do i have", "is there", "am i")
    if not (low.endswith("?") or low.startswith(starts)):
        return None
    d = _day_word_date(S, text)
    if d is None:
        d = _now_dt(S).date()
    found = []
    for it in S["items"]:
        s = _parse_iso(it.get("start"))
        if s is not None and s.date() == d:
            found.append(it)
    found.sort(key=lambda x: _parse_iso(x.get("start")) or datetime(2100, 1, 1))
    dayname = d.strftime("%A")
    if not found:
        txt = "%s looks free." % dayname
    else:
        parts = []
        for it in found:
            s = _parse_iso(it.get("start"))
            if it.get("window"):
                parts.append("%s %s" % (it.get("title"), it.get("window")))
            elif s is not None:
                parts.append("%s at %s" % (it.get("title"), s.strftime("%H:%M")))
            else:
                parts.append(it.get("title"))
        txt = "On %s: %s." % (dayname, ", ".join(parts))
    msg = _reply(S, txt)
    _add_log(S, "query", [it["id"] for it in found], {"text": text}, "typed")
    return {"messages": [msg], "created": []}


def _try_cancel(S, low, text):
    starts = ("cancel", "delete", "remove", "drop", "scrap", "skip",
              "get rid of", "forget", "i don't need")
    if not low.startswith(starts):
        return None
    it = _best_match(S, text)
    if it is None:
        msg = _info(S, "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    plan = {
        "id": _new_id("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": [{"op": "delete", "item_id": it["id"], "before": _copy_item(it), "after": None}],
        "affected": [it["id"]],
    }
    S["plans"][plan["id"]] = plan
    msg = _info(S, "I can remove '%s' for you. Apply the change?" % it.get("title"))
    return {"messages": [msg], "created": [], "plan": plan}


def _try_move(S, low, text):
    starts = ("move", "reschedule", "push", "shift", "postpone", "bump",
              "delay", "put off", "pull")
    if not low.startswith(starts):
        return None
    it = _best_match(S, text)
    if it is None:
        msg = _info(S, "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    s = _parse_iso(it.get("start"))
    e = _parse_iso(it.get("end"))
    if s is None:
        msg = _info(S, "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    new_s = None
    new_e = None
    m = re.search(r"\b(\d+)\s+weeks?\b", low)
    if m:
        days = 7 * int(m.group(1))
        new_s = s + timedelta(days=days)
        new_e = e + timedelta(days=days) if e else None
    elif re.search(r"\b(?:next week|a week|back a week|by a week|a week later|one week)\b", low):
        new_s = s + timedelta(days=7)
        new_e = e + timedelta(days=7) if e else None
    elif re.search(r"\btomorrow\b", low):
        new_s = s + timedelta(days=1)
        new_e = e + timedelta(days=1) if e else None
    elif re.search(r"\b(?:a day|back a day|one day)\b", low):
        new_s = s + timedelta(days=1)
        new_e = e + timedelta(days=1) if e else None
    else:
        m = re.search(r"\b(\d+)\s+days?\b", low)
        if m:
            days = int(m.group(1))
            new_s = s + timedelta(days=days)
            new_e = e + timedelta(days=days) if e else None
    if new_s is None:
        for name, idx in WEEKDAYS.items():
            if re.search(r"\b" + name + r"\b", low):
                delta = (idx - s.weekday()) % 7
                if delta == 0:
                    delta = 7
                new_s = s + timedelta(days=delta)
                new_e = e + timedelta(days=delta) if e else None
                break
    if new_s is None:
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", low)
        if m:
            h = int(m.group(1))
            mi = int(m.group(2)) if m.group(2) else 0
            ap = m.group(3)
            if ap == "pm" and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            new_s = datetime.combine(s.date(), dtime(h, mi))
            if e:
                length = e - s
                new_e = new_s + length
    if new_s is None:
        msg = _info(S, "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    undo_id = _new_id("undo_")
    S["undo"][undo_id] = {
        "item_id": it["id"],
        "start": it.get("start"),
        "end": it.get("end"),
        "window": it.get("window"),
        "postpone_count": it.get("postpone_count", 0),
    }
    it["start"] = _fmt_iso(new_s)
    if new_e is not None:
        it["end"] = _fmt_iso(new_e)
    _add_log(S, "update", [it["id"]], {"old_start": s.strftime("%Y-%m-%dT%H:%M"),
                                        "new_start": it["start"]}, "typed")
    msg = _info(S, "Moved '%s' to %s. Undo if you'd rather keep it." % (
        it.get("title"), new_s.strftime("%a %b %d at %H:%M")), item_id=it["id"])
    msg["undo_id"] = undo_id
    return {"messages": [msg], "created": []}


# === SECTION: plans ===

def apply_plan(S, plan_id):
    plan = S["plans"].get(plan_id)
    if plan is None:
        raise KeyError(plan_id)
    if plan.get("status") != "preview":
        return {"plan": plan}
    created_ids = []
    for op in plan["ops"]:
        if op["op"] == "delete":
            it = _find_item(S, op["item_id"])
            if it is not None:
                S["items"].remove(it)
            _add_log(S, "delete", [op["item_id"]], {"plan": plan_id}, "agent")
        elif op["op"] == "create":
            new_it = _copy_item(op["after"])
            S["items"].append(new_it)
            created_ids.append(new_it["id"])
            _add_log(S, "create", [new_it["id"]], {"plan": plan_id}, "agent")
        elif op["op"] == "update":
            it = _find_item(S, op["item_id"])
            if it is not None:
                idx = S["items"].index(it)
                S["items"][idx] = _copy_item(op["after"])
            _add_log(S, "update", [op["item_id"]], {"plan": plan_id}, "agent")
    plan["status"] = "applied"
    _add_log(S, "plan_applied", list(plan["affected"]) + created_ids, {"plan": plan_id}, "agent")
    return {"plan": plan}


def undo_plan(S, plan_id):
    plan = S["plans"].get(plan_id)
    if plan is None:
        raise KeyError(plan_id)
    if plan.get("status") != "applied":
        return {"plan": plan}
    for op in plan["ops"]:
        if op["op"] == "delete":
            if op.get("before") is not None:
                S["items"].append(_copy_item(op["before"]))
        elif op["op"] == "create":
            it = _find_item(S, op["after"]["id"])
            if it is not None:
                S["items"].remove(it)
        elif op["op"] == "update":
            it = _find_item(S, op["item_id"])
            if it is not None and op.get("before") is not None:
                idx = S["items"].index(it)
                S["items"][idx] = _copy_item(op["before"])
    plan["status"] = "undone"
    _add_log(S, "plan_undone", list(plan["affected"]), {"plan": plan_id}, "agent")
    return {"plan": plan}


# === SECTION: import ===

def import_file(S, filename, content):
    if content is None:
        content = ""
    is_ics = "BEGIN:VCALENDAR" in content or (filename or "").lower().endswith(".ics")
    if is_ics:
        return _import_ics(S, filename, content)
    return _import_text(S, filename, content)


def _import_ics(S, filename, content):
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    events = []
    cur = None
    for line in lines:
        if line.strip() == "BEGIN:VEVENT":
            cur = {}
        elif line.strip() == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None:
            if ":" in line:
                head, val = line.split(":", 1)
                name = head.split(";")[0].strip().upper()
                cur[name] = (head, val.strip())
    ops = []
    for ev in events:
        summary = ev.get("SUMMARY", (None, ""))[1] or "Untitled"
        uid = ev.get("UID", (None, None))[1]
        rrule = ev.get("RRULE", (None, None))[1]
        dtstart = ev.get("DTSTART")
        dtend = ev.get("DTEND")
        if dtstart is None:
            continue
        start, all_day = _parse_ics_dt(dtstart)
        if start is None:
            continue
        if dtend is not None:
            end, _ = _parse_ics_dt(dtend)
        else:
            end = start + timedelta(days=1) if all_day else start + timedelta(minutes=60)
        if end is None:
            end = start + timedelta(minutes=60)
        if all_day and end <= start:
            end = start + timedelta(days=1)
        dup = False
        for it in S["items"]:
            if uid and it.get("uid") == uid:
                dup = True
                break
            if (it.get("title") or "").lower() == summary.lower() and it.get("start") == _fmt_iso(start):
                dup = True
                break
        if dup:
            continue
        new_it = _default_item()
        new_it["id"] = _new_id("itm_")
        new_it["kind"] = "event"
        new_it["title"] = summary
        new_it["start"] = _fmt_iso(start)
        new_it["end"] = _fmt_iso(end)
        new_it["window"] = "all day" if all_day else None
        new_it["source"] = "ics"
        new_it["uid"] = uid
        new_it["recurrence"] = rrule
        ops.append({"op": "create", "item_id": None, "before": None, "after": new_it})
    plan = {
        "id": _new_id("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": [],
    }
    S["plans"][plan["id"]] = plan
    _add_log(S, "import", [], {"filename": filename, "ops": len(ops)}, "ics")
    return {"plan": plan}


def _parse_ics_dt(pair):
    head, val = pair
    val = val.strip()
    all_day = "VALUE=DATE" in head.upper() or (len(val) == 8 and "T" not in val)
    if all_day:
        try:
            d = datetime.strptime(val[:8], "%Y%m%d")
            return (d, True)
        except Exception:
            return (None, True)
    m = re.match(r"(\d{8})T(\d{4})(\d{2})?", val)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d")
            h = int(m.group(2)[:2])
            mi = int(m.group(2)[2:4])
            return (datetime.combine(d.date(), dtime(h, mi)), False)
        except Exception:
            return (None, False)
    return (None, False)


def _import_text(S, filename, content):
    text = content.replace("\r\n", "\n")
    sentences = re.split(r"[.!?\n]+", text)
    ops = []
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        low = s.lower()
        has_clear = any(p in low for p in ["away", "clear", "free", "cancel", "off"])
        rng = _range_for(S, s)
        if has_clear and rng is not None:
            start, end, label = rng
            for it in _open_items(S):
                if _overlaps(it, start, end):
                    ops.append({"op": "delete", "item_id": it["id"],
                                "before": _copy_item(it), "after": None})
            continue
        d = _day_word_date(S, s)
        has_time = bool(re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", low)) or \
            bool(re.search(r"\b(?:noon|morning|afternoon|evening|tonight|lunch)\b", low))
        if d is None and not has_time:
            continue
        if d is None:
            d = _now_dt(S).date()
        start = None
        window = None
        if re.search(r"\bnoon\b", low) or re.search(r"\blunch\b", low):
            start = datetime.combine(d, dtime(12, 0))
            window = "around noon"
        elif re.search(r"\bmorning\b", low):
            start = datetime.combine(d, dtime(9, 0))
            window = "morning"
        elif re.search(r"\bafternoon\b", low):
            start = datetime.combine(d, dtime(14, 0))
            window = "afternoon"
        elif re.search(r"\bevening\b", low) or re.search(r"\btonight\b", low):
            start = datetime.combine(d, dtime(18, 0))
            window = "evening"
        else:
            m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", low)
            if m:
                h = int(m.group(1))
                mi = int(m.group(2)) if m.group(2) else 0
                ap = m.group(3)
                if ap == "pm" and h < 12:
                    h += 12
                if ap == "am" and h == 12:
                    h = 0
                if h < 8 and ap is None:
                    h += 12
                start = datetime.combine(d, dtime(h, mi))
        if start is None:
            start = datetime.combine(d, dtime(9, 0))
        title = s
        title = re.sub(r"\b(?:also|on|at)\b", " ", title, flags=re.I)
        for name in list(WEEKDAYS.keys()) + list(MONTHS.keys()):
            title = re.sub(r"\b" + name + r"\b", " ", title, flags=re.I)
        title = re.sub(r"\b(?:today|tomorrow|noon|morning|afternoon|evening|tonight|around)\b", " ", title, flags=re.I)
        title = re.sub(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", " ", title, flags=re.I)
        title = re.sub(r"\b\d{1,2}(?:st|nd|rd|th)\b", " ", title, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip(" ,.")
        if not title:
            title = "New item"
        title = title[0].upper() + title[1:]
        kind = "event" if re.search(r"\b(?:with|lunch|dinner|meet|call)\b", low) else "task"
        new_it = _default_item()
        new_it["id"] = _new_id("itm_")
        new_it["kind"] = kind
        new_it["title"] = title
        new_it["start"] = _fmt_iso(start)
        new_it["end"] = _fmt_iso(start + timedelta(minutes=60))
        new_it["window"] = window
        new_it["source"] = "text"
        ops.append({"op": "create", "item_id": None, "before": None, "after": new_it})
    plan = {
        "id": _new_id("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": [op["item_id"] for op in ops if op["op"] != "create"],
    }
    S["plans"][plan["id"]] = plan
    _add_log(S, "import", list(plan["affected"]), {"filename": filename, "ops": len(ops)}, "text")
    return {"plan": plan}


# === SECTION: views ===

def view_state(S):
    v = S.get("view") or {"type": "week", "params": {}}
    vtype = v.get("type", "week")
    params = v.get("params", {})
    if vtype == "custom" and v.get("spec"):
        return {
            "type": "custom",
            "params": params,
            "spec": v["spec"],
            "hidden_counts": v3.hidden_counts(S, v["spec"]),
        }
    now = _now_dt(S)
    today = now.date()
    hidden = {}
    for it in S["items"]:
        s = _parse_iso(it.get("start"))
        if s is None:
            continue
        d = s.date()
        hide = False
        if vtype == "workweek":
            hide = d.weekday() >= 5
        elif vtype == "day":
            hide = d != today
        elif vtype == "week":
            monday = today - timedelta(days=today.weekday())
            sunday = monday + timedelta(days=6)
            hide = d < monday or d > sunday
        elif vtype == "month":
            hide = (d.year != today.year or d.month != today.month)
        elif vtype == "custom":
            if params.get("hide_weekends"):
                hide = d.weekday() >= 5
        if hide:
            key = d.strftime("%Y-%m-%d")
            hidden[key] = hidden.get(key, 0) + 1
    return {"type": vtype, "params": params, "hidden_counts": hidden}


def set_view(S, view_type):
    if view_type not in ("month", "week", "workweek", "day"):
        raise KeyError(view_type)
    S["view"] = {"type": view_type, "params": {}}
    _add_log(S, "view_switch", [], {"type": view_type}, "person")
    return {"view": view_state(S)}


def meta_state(S):
    m = S.get("meta", {})
    return {
        "novelty": m.get("novelty", 0.6),
        "cadence_hours": m.get("cadence_hours", 24),
        "breakdown_appetite": m.get("breakdown_appetite", 0.5),
        "tone": m.get("tone", "gentle"),
        "threshold": m.get("threshold", 0.7),
    }


# === SECTION: habit learner ===

def _history(S):
    hist = []
    for it in S["items"]:
        if it.get("status") == "done":
            t = _parse_iso(it.get("end")) or _parse_iso(it.get("start"))
            if t is not None:
                hist.append((t, it["id"]))
    for e in S["log"]:
        if e.get("kind") == "done":
            t = _parse_iso(e.get("at"))
            if t is not None:
                hist.append((t, e["id"]))
    return hist


def _store_belief(S, key, level, statement, value, share, n, evidence):
    if key in S["deleted_beliefs"]:
        return None
    conf = max(0.0, share * min(1.0, n / 6.0) - S["penalty"].get(key, 0.0))
    conf = round(conf, 2)
    old = S["beliefs"].get(key)
    belief = {
        "key": key,
        "level": level,
        "statement": statement,
        "value": value,
        "confidence": conf,
        "evidence": list(evidence)[:20],
        "updated_at": S.get("now"),
    }
    S["beliefs"][key] = belief
    if old is None or abs(old.get("confidence", 0) - conf) >= 0.05:
        _activity(S, "learner", "belief_update",
                  "%s (confidence %.2f)" % (statement, conf))
    if old is not None and old.get("confidence", 0) < 0.7 <= conf:
        _activity(S, "learner", "threshold_crossed",
                  "%s crossed the proposal threshold (confidence %.2f)" % (statement, conf))
    return belief


def learn_beliefs(S):
    hist = _history(S)
    n = len(hist)
    if "evening_work" not in S["deleted_beliefs"] or "morning_work" not in S["deleted_beliefs"]:
        if n >= 5:
            k = sum(1 for t, _ in hist if t.hour >= 20)
            share = k / n
            if share >= 0.5:
                _store_belief(S, "evening_work", "habit",
                              "Finishes tasks after 8 PM (%d of %d)" % (k, n),
                              20, share, n, [eid for _, eid in hist])
            else:
                km = sum(1 for t, _ in hist if t.hour < 12)
                if km / n >= 0.5:
                    _store_belief(S, "morning_work", "habit",
                                  "Gets things done in the morning (%d of %d)" % (km, n),
                                  9, km / n, n, [eid for _, eid in hist])
    if "no_weekend" not in S["deleted_beliefs"]:
        weeks = set()
        weekend = 0
        ids = []
        total = 0
        for it in S["items"]:
            s = _parse_iso(it.get("start"))
            if s is None:
                continue
            total += 1
            weeks.add(s.isocalendar()[0] * 100 + s.isocalendar()[1])
            if s.weekday() >= 5:
                weekend += 1
            ids.append(it["id"])
        if len(weeks) >= 4 and weekend == 0 and total > 0:
            w = len(weeks)
            _store_belief(S, "no_weekend", "habit",
                          "No weekend plans in %d weeks (0 of %d items)" % (w, total),
                          True, 1.0, w, ids)
    if "block_length" not in S["deleted_beliefs"]:
        lengths = []
        ids = []
        for it in S["items"]:
            if it.get("status") != "done":
                continue
            s = _parse_iso(it.get("start"))
            e = _parse_iso(it.get("end"))
            if s is not None and e is not None:
                lengths.append(int((e - s).total_seconds() // 60))
                ids.append(it["id"])
        if len(lengths) >= 6:
            sl = sorted(lengths)
            m = sl[len(sl) // 2]
            k = sum(1 for x in lengths if abs(x - m) <= 15)
            _store_belief(S, "block_length", "habit",
                          "Tasks usually take about %d minutes (%d of %d)" % (m, k, len(lengths)),
                          m, k / len(lengths), len(lengths), ids)


def get_beliefs(S):
    return {"beliefs": list(S["beliefs"].values())}


def delete_belief(S, key):
    if key in S["beliefs"]:
        del S["beliefs"][key]
    if key not in S["deleted_beliefs"]:
        S["deleted_beliefs"].append(key)
    _add_log(S, "belief_deleted", [], {"key": key}, "person")
    _activity(S, "learner", "belief_deleted", "Removed the belief '%s'." % key)
    return {"ok": True}


# === SECTION: proposal gate ===

def _candidate_beliefs(S):
    threshold = S["meta"].get("threshold", 0.7)
    cands = []
    for key, b in S["beliefs"].items():
        if b.get("level") != "habit":
            continue
        if b.get("confidence", 0) < threshold:
            continue
        if key in S["blocked"] and b.get("confidence", 0) <= S["blocked"][key] + 0.15:
            continue
        cands.append(b)
    order = {"no_weekend": 0, "evening_work": 1, "morning_work": 2, "block_length": 3}
    cands.sort(key=lambda b: (order.get(b["key"], 9), -b.get("confidence", 0)))
    return cands


def _classic_view(S, belief):
    key = belief["key"]
    if key == "no_weekend":
        return {"type": "workweek", "params": {}}
    if key == "evening_work":
        return {"type": "day", "params": {"focus": "evening"}}
    if key == "morning_work":
        return {"type": "day", "params": {"focus": "morning"}}
    if key == "block_length":
        return {"type": "week", "params": {"block_min": belief.get("value")}}
    return {"type": "week", "params": {}}


def _proposal_title(belief):
    key = belief["key"]
    if key == "no_weekend":
        return "Hide weekends"
    if key == "evening_work":
        return "Evening lane"
    if key == "morning_work":
        return "Morning lane"
    if key == "block_length":
        return "Block lengths"
    return "A new view"


def run_gate(S):
    for p in S["proposals"]:
        if p.get("status") == "shown":
            return p
    now = _now_dt(S)
    last = S.get("last_proposal_at")
    cadence = S["meta"].get("cadence_hours", 24)
    if last is not None:
        last_dt = _parse_iso(last)
        if last_dt is not None and (now - last_dt) < timedelta(hours=cadence):
            return None
    cands = _candidate_beliefs(S)
    if not cands:
        return None
    belief = cands[0]
    key = belief["key"]
    novelty = "invented" if len(S["invented_reverts"]) < 2 else "classic"
    if novelty == "invented":
        spec = v3.best_spec(S, key)
        view = {
            "type": "custom",
            "params": {"belief": key},
            "spec": spec,
            "hidden_counts": v3.hidden_counts(S, spec),
        }
    else:
        view = _classic_view(S, belief)
        view["hidden_counts"] = view_state(S)["hidden_counts"]
    versions = {}
    for p in S["prompts"]:
        if p.get("active"):
            versions[p["agent"]] = p["version"]
    prop = {
        "id": _new_id("prop_"),
        "title": _proposal_title(belief),
        "reason": "%s. %s?" % (belief["statement"], _proposal_title(belief)),
        "view": view,
        "belief_keys": [key],
        "novelty": novelty,
        "prompt_versions": versions,
        "status": "shown",
    }
    S["proposals"].append(prop)
    S["last_proposal_at"] = S.get("now")
    _add_log(S, "proposal_shown", [], {"proposal": prop["id"], "belief": key}, "agent")
    _activity(S, "designer", "proposal_shown", "Showed the idea '%s'." % prop["title"])
    return prop


def get_proposal(S):
    for p in S["proposals"]:
        if p.get("status") == "shown":
            return {"proposal": p}
    return {"proposal": None}


def proposal_action(S, proposal_id, action):
    prop = None
    for p in S["proposals"]:
        if p.get("id") == proposal_id:
            prop = p
            break
    if prop is None:
        raise KeyError(proposal_id)
    key = prop["belief_keys"][0] if prop.get("belief_keys") else None
    if action == "accept" and prop.get("status") == "shown":
        prop["prev_view"] = json.loads(json.dumps(S["view"]))
        S["view"] = json.loads(json.dumps(prop["view"]))
        prop["status"] = "accepted"
        prop["accepted_at"] = S.get("now")
        prop["kept_credit"] = False
        S["dismiss_streak"] = 0
        _add_log(S, "proposal_accepted", [], {"proposal": proposal_id}, "person")
        _activity(S, "designer", "proposal_accepted", "You tried '%s'." % prop["title"])
    elif action == "undo" and prop.get("status") == "accepted":
        prev = prop.get("prev_view") or {"type": "week", "params": {}}
        S["view"] = json.loads(json.dumps(prev))
        prop["status"] = "reverted"
        if key:
            S["penalty"][key] = S["penalty"].get(key, 0.0) + 0.2
            b = S["beliefs"].get(key)
            if b is not None:
                b["confidence"] = round(max(0.0, b.get("confidence", 0) - 0.2), 2)
                S["blocked"][key] = b["confidence"]
        entry = _add_log(S, "proposal_reverted", [], {"proposal": proposal_id}, "person")
        if prop.get("novelty") == "invented":
            S["invented_reverts"].append(entry["id"])
            S["meta"]["novelty"] = round(max(0.0, S["meta"].get("novelty", 0.6) - 0.1), 2)
            if len(S["invented_reverts"]) % 2 == 0:
                _meta_learn(S)
        _activity(S, "designer", "proposal_reverted", "You went back from '%s'." % prop["title"])
    elif action == "dismiss" and prop.get("status") == "shown":
        prop["status"] = "dismissed"
        if key:
            b = S["beliefs"].get(key)
            if b is not None:
                S["blocked"][key] = b.get("confidence", 0)
        S["dismiss_streak"] = S.get("dismiss_streak", 0) + 1
        _add_log(S, "proposal_dismissed", [], {"proposal": proposal_id}, "person")
        _activity(S, "designer", "proposal_dismissed", "You set '%s' aside for now." % prop["title"])
        if S["dismiss_streak"] >= 3:
            old_t = S["meta"].get("threshold", 0.7)
            old_c = S["meta"].get("cadence_hours", 24)
            new_t = round(min(0.95, old_t + 0.1), 2)
            new_c = old_c * 2
            S["meta"]["threshold"] = new_t
            S["meta"]["cadence_hours"] = new_c
            S["dismiss_streak"] = 0
            _activity(S, "designer", "knob_change",
                      "Raised the proposal threshold from %.2f to %.2f and cadence from %g to %g hours: 3 proposals in a row were turned down." % (
                          old_t, new_t, old_c, new_c))
    return {"proposal": prop, "view": view_state(S)}


def _meta_learn(S):
    prev = None
    for p in S["prompts"]:
        if p["agent"] == "designer" and p.get("active"):
            prev = p
    if prev is None:
        return
    line = "- Prefer classic views (workweek, week, day): 2 invented views were reverted."
    learned = prev.get("learned", "")
    if learned:
        learned = learned + "\n" + line
    else:
        learned = line
    lines = learned.split("\n")[:12]
    learned = "\n".join(lines)
    for bad in ["ignore", "override", "core rules"]:
        if bad in learned.lower():
            learned = learned.replace(bad, "respect")
    new_version = {
        "agent": "designer",
        "version": prev["version"] + 1,
        "core_hash": prev["core_hash"],
        "learned": learned,
        "diff": "+ " + line,
        "reason": "2 invented proposals were reverted",
        "evidence": list(S["invented_reverts"]),
        "replay": {"passed": True, "detail": "learned text checked: <= 12 lines, no rule override"},
        "active": True,
    }
    prev["active"] = False
    S["prompts"].append(new_version)
    S["beliefs"]["prefers_classic"] = {
        "key": "prefers_classic",
        "level": "meta",
        "statement": "Prefers classic views (2 of 2 invented views reverted)",
        "value": "classic",
        "confidence": 0.8,
        "evidence": list(S["invented_reverts"]),
        "updated_at": S.get("now"),
    }
    _activity(S, "designer", "prompt_rewrite",
              "designer v%d: %s" % (new_version["version"], new_version["diff"]),
              extra={"diff": new_version["diff"]})


def get_prompts(S):
    return {"prompts": list(S["prompts"])}


def rollback(S, agent):
    versions = [p for p in S["prompts"] if p["agent"] == agent]
    versions.sort(key=lambda p: p["version"])
    active = None
    for p in versions:
        if p.get("active"):
            active = p
    if active is None:
        if versions:
            versions[0]["active"] = True
            active = versions[0]
        else:
            raise KeyError(agent)
    else:
        idx = versions.index(active)
        if idx > 0:
            active["active"] = False
            versions[idx - 1]["active"] = True
            active = versions[idx - 1]
    _activity(S, agent, "prompt_rollback", "Rolled %s back to v%d." % (agent, active["version"]))
    return {"active": active}


def get_activity(S):
    return {"events": list(S["activity"])}


# === SECTION: learn / tick ===

def learn(S):
    learn_beliefs(S)
    prop = run_gate(S)
    return {"beliefs": list(S["beliefs"].values()), "proposal": prop}


def tick(S, on_clock):
    now = _now_dt(S)
    for p in S["proposals"]:
        if p.get("status") == "accepted" and not p.get("kept_credit"):
            acc = _parse_iso(p.get("accepted_at"))
            if acc is not None and (now - acc) >= timedelta(hours=72):
                p["kept_credit"] = True
                old_t = S["meta"].get("threshold", 0.7)
                new_t = round(max(0.5, old_t - 0.05), 2)
                S["meta"]["threshold"] = new_t
                _activity(S, "designer", "knob_change",
                          "Lowered the proposal threshold from %.2f to %.2f: you kept the view '%s' for 3 days." % (
                              old_t, new_t, p.get("title")))
    if on_clock or (len(S["log"]) - S.get("learn_mark", 0)) >= 5:
        S["learn_mark"] = len(S["log"])
        learn_beliefs(S)
        run_gate(S)


def get_log(S):
    return {"entries": list(S["log"])}
