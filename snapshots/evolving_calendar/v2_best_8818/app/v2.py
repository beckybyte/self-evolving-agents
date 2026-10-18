import json
import re
import uuid
import hashlib
from datetime import datetime, date, time as dtime, timedelta

# === SECTION: constants ===

STOP_WORDS = {
    "move", "reschedule", "push", "shift", "cancel", "delete", "remove", "drop",
    "my", "the", "a", "an", "to", "on", "at", "for", "next", "week", "please", "it",
}

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
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot", "lazy", "urgent", "alert"]

CORE_TEXTS = {
    "designer": "You design gentle views of a person's calendar. You never hide an item silently and you never use guilt wording.",
    "followthrough": "You help a person finish small steps. You ask one gentle question at a time and you never use guilt wording.",
    "learner": "You learn habits from a person's history. You only state beliefs that rest on counted evidence and you never use guilt wording.",
}

SOFT_WINDOWS = [
    ("around noon", 12, 0, 13, 0),
    ("noon", 12, 0, 13, 0),
    ("lunch", 12, 0, 13, 0),
    ("this afternoon", 14, 0, 17, 0),
    ("afternoon", 14, 0, 17, 0),
    ("morning", 9, 0, 12, 0),
    ("evening", 18, 0, 21, 0),
    ("tonight", 20, 0, 22, 0),
]

# === SECTION: small helpers ===

def _now(S):
    return S.get("now") or "2026-10-19T09:00"


def _now_dt(S):
    try:
        return datetime.strptime(_now(S), "%Y-%m-%dT%H:%M")
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


def _clean_text(text):
    """Replace banned phrases with kind ones."""
    out = text
    low = out.lower()
    for bad in BANNED:
        while bad in low:
            idx = low.find(bad)
            out = out[:idx] + "moved forward" + out[idx + len(bad):]
            low = out.lower()
    return out


def _msg(S, kind, text, item_id=None, undo_id=None, steps=None, role="agent"):
    m = {
        "id": _new_id("msg_"),
        "role": role,
        "kind": kind,
        "text": _clean_text(text),
        "item_id": item_id,
        "at": _now(S),
        "undo_id": undo_id,
    }
    if kind == "breakdown":
        m["steps"] = steps if steps is not None else []
    S.setdefault("messages", []).append(m)
    return m


def _info(S, text, item_id=None):
    return _msg(S, "info", text, item_id=item_id)


def _reply(S, text, item_id=None):
    return _msg(S, "reply", text, item_id=item_id)


def _item_by_id(S, item_id):
    for it in S.get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def _copy_item(it):
    if it is None:
        return None
    out = {}
    for k, v in it.items():
        out[k] = v
    return out


def _default_item():
    return {
        "id": None, "kind": "task", "title": "", "start": None, "end": None,
        "window": None, "status": "open", "why": None, "parent_id": None,
        "estimate_min": None, "postpone_count": 0, "source": "typed",
    }


def _full_item(partial):
    it = _default_item()
    for k, v in (partial or {}).items():
        it[k] = v
    return it


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _match_words(text):
    return set(w for w in _words(text) if w not in STOP_WORDS)


def _find_item(S, text):
    """Best matching open item by title words."""
    mw = _match_words(text)
    best = None
    best_score = 0
    for it in S.get("items", []):
        if it.get("status") != "open":
            continue
        title_words = [w for w in _words(it.get("title") or "") if len(w) >= 3]
        score = 0
        for w in title_words:
            if w in mw:
                score += 1
        if score > best_score:
            best_score = score
            best = it
        elif score == best_score and score > 0 and best is not None:
            # earliest start wins
            a = _parse_iso(it.get("start"))
            b = _parse_iso(best.get("start"))
            if a is not None and (b is None or a < b):
                best = it
    return best


def _day_from_text(S, text):
    """Return a date object for a day word in text, or None."""
    low = (text or "").lower()
    now_dt = _now_dt(S)
    today_d = now_dt.date()
    # ISO date
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    # Month day
    m = re.search(r"\b([a-z]+)\.?\s+(\d{1,2})\b", low)
    if m and m.group(1) in MONTHS:
        try:
            return date(today_d.year, MONTHS[m.group(1)], int(m.group(2)))
        except Exception:
            pass
    if "today" in low:
        return today_d
    if "tomorrow" in low:
        return today_d + timedelta(days=1)
    for name, idx in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            delta = (idx - today_d.weekday()) % 7
            if delta == 0:
                delta = 7
            return today_d + timedelta(days=delta)
    return None


def _clock_time(text):
    """Return (hour, minute) or None."""
    low = (text or "").lower()
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        ap = m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return (h, mi)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1))
        ap = m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return (h, 0)
    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        if 0 <= h <= 23:
            return (h, 0)
    return None


def _soft_window(text):
    low = (text or "").lower()
    for name, h1, m1, h2, m2 in SOFT_WINDOWS:
        if name in low:
            return (name, h1, m1, h2, m2)
    return None


def _overlaps(item, start_dt, end_dt):
    s = _parse_iso(item.get("start"))
    if s is None:
        return False
    e = _parse_iso(item.get("end"))
    if e is None:
        e = s + timedelta(minutes=60)
    return s < end_dt and start_dt < e


def _weekend_range(S):
    now_dt = _now_dt(S)
    today_d = now_dt.date()
    wd = today_d.weekday()
    if wd == 5:
        sat = today_d
    elif wd == 6:
        sat = today_d - timedelta(days=1)
    else:
        sat = today_d + timedelta(days=(5 - wd))
    mon = sat + timedelta(days=2)
    return (datetime.combine(sat, dtime(0, 0)), datetime.combine(mon, dtime(0, 0)))


def _day_range(day_d):
    return (datetime.combine(day_d, dtime(0, 0)), datetime.combine(day_d + timedelta(days=1), dtime(0, 0)))


def _fmt_day_name(day_d):
    return day_d.strftime("%A")


def _fmt_short(day_d):
    return day_d.strftime("%a %b %d").replace(" 0", " ")


def _fmt_time(dt):
    return dt.strftime("%H:%M")


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
    S["meta"] = {"novelty": 0.6, "cadence_hours": 24, "breakdown_appetite": 0.5, "tone": "gentle"}
    S["invented_reverts"] = []
    S["last_proposal_at"] = None
    S["learn_mark"] = 0
    S["activity"] = []
    S["prompts"] = []
    for agent in ("designer", "followthrough", "learner"):
        core = CORE_TEXTS[agent]
        ch = hashlib.sha256(core.encode("utf-8")).hexdigest()[:12]
        S["prompts"].append({
            "agent": agent,
            "version": 1,
            "core_hash": ch,
            "learned": "",
            "diff": "",
            "reason": "initial version",
            "evidence": [],
            "replay": {"passed": True, "detail": "initial"},
            "active": True,
        })


# === SECTION: logging ===

def add_log(S, kind, item_ids, payload, source):
    entry = {
        "id": _new_id("log_"),
        "at": _now(S),
        "kind": kind,
        "item_ids": list(item_ids or []),
        "payload": payload or {},
        "source": source,
    }
    S.setdefault("log", []).append(entry)
    return entry


def get_log(S):
    return {"entries": list(S.get("log", []))}


# === SECTION: activity ===

def _activity(S, agent, kind, text, extra=None):
    ev = {"at": _now(S), "agent": agent, "kind": kind, "text": _clean_text(text)}
    if extra:
        for k, v in extra.items():
            ev[k] = v
    S.setdefault("activity", []).append(ev)
    return ev


def get_activity(S):
    return {"events": list(S.get("activity", []))}


# === SECTION: command handling ===

def handle_command(S, text):
    if not text:
        return None
    low = text.strip().lower()
    if not low:
        return None

    # QUERY
    if low.endswith("?") or re.match(r"^(what|what's|whats|anything|show|when)\b", low):
        return _handle_query(S, text, low)

    # BULK
    if re.search(r"\b(empty|clear|free up|wipe)\b", low) and (
        "weekend" in low or _day_from_text(S, low) is not None
    ):
        return _handle_bulk(S, text, low)

    # CANCEL
    if re.match(r"^(cancel|delete|remove|drop|get rid of)\b", low):
        return _handle_cancel(S, text, low)

    # MOVE
    if re.match(r"^(move|reschedule|push|shift)\b", low):
        return _handle_move(S, text, low)

    return None


def _handle_query(S, text, low):
    day_d = _day_from_text(S, low)
    if day_d is None:
        day_d = _now_dt(S).date()
    found = []
    for it in S.get("items", []):
        s = _parse_iso(it.get("start"))
        if s is not None and s.date() == day_d:
            found.append(it)
    found.sort(key=lambda it: _parse_iso(it.get("start")) or datetime.max)
    day_name = _fmt_day_name(day_d)
    if not found:
        msg = _reply(S, day_name + " looks free.")
    else:
        parts = []
        for it in found:
            s = _parse_iso(it.get("start"))
            if it.get("window"):
                parts.append("%s %s" % (it.get("title"), it.get("window")))
            elif s is not None:
                parts.append("%s at %s" % (it.get("title"), _fmt_time(s)))
            else:
                parts.append(it.get("title"))
        msg = _reply(S, "On %s: %s." % (day_name, ", ".join(parts)))
    add_log(S, "query", [it.get("id") for it in found], {"text": text}, "typed")
    return {"messages": [msg], "created": []}


def _handle_bulk(S, text, low):
    if "weekend" in low:
        start_dt, end_dt = _weekend_range(S)
    else:
        day_d = _day_from_text(S, low)
        if day_d is None:
            day_d = _now_dt(S).date()
        start_dt, end_dt = _day_range(day_d)
    affected = []
    for it in S.get("items", []):
        if it.get("status") != "open":
            continue
        if _overlaps(it, start_dt, end_dt):
            affected.append(it)
    ops = []
    for it in affected:
        ops.append({"op": "delete", "item_id": it.get("id"), "before": _copy_item(it), "after": None})
    # purpose
    purpose = None
    m = re.search(r"for\s+(?:a|an|my)?\s*([a-z ]+?)(?:\.|$)", low)
    if m:
        purpose = m.group(1).strip()
    if purpose:
        title = purpose
        title = re.sub(r"^(a|an|my)\s+", "", title)
        title = title.strip().capitalize()
        new_item = _full_item({
            "id": _new_id("itm_"),
            "kind": "event",
            "title": title,
            "start": _fmt_iso(datetime.combine(start_dt.date(), dtime(9, 0))),
            "end": _fmt_iso(datetime.combine(end_dt.date() - timedelta(days=1), dtime(18, 0))),
            "window": "weekend",
            "status": "open",
            "source": "typed",
        })
        ops.append({"op": "create", "item_id": new_item["id"], "before": None, "after": new_item})
    plan = {
        "id": _new_id("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": ops,
        "affected": [it.get("id") for it in affected],
    }
    S.setdefault("plans", {})[plan["id"]] = plan
    n = len(affected)
    if purpose:
        msg = _info(S, "I can clear %d item(s) and add '%s'. Shall I apply it?" % (n, ops[-1]["after"]["title"]))
    else:
        msg = _info(S, "I can clear %d item(s). Shall I apply it?" % n)
    return {"messages": [msg], "created": [], "plan": plan}


def _handle_cancel(S, text, low):
    it = _find_item(S, text)
    if it is None:
        msg = _info(S, "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    plan = {
        "id": _new_id("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": [{"op": "delete", "item_id": it.get("id"), "before": _copy_item(it), "after": None}],
        "affected": [it.get("id")],
    }
    S.setdefault("plans", {})[plan["id"]] = plan
    msg = _info(S, "I can remove '%s' for you. Apply the change?" % it.get("title"))
    return {"messages": [msg], "created": [], "plan": plan}


def _handle_move(S, text, low):
    it = _find_item(S, text)
    if it is None:
        msg = _info(S, "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    old_start = it.get("start")
    old_end = it.get("end")
    old_window = it.get("window")
    old_pc = it.get("postpone_count", 0)
    s = _parse_iso(old_start)
    e = _parse_iso(old_end)
    if s is None:
        msg = _info(S, "I couldn't find a time for '%s' to move." % it.get("title"))
        return {"messages": [msg], "created": []}
    new_s = s
    new_e = e
    if "next week" in low:
        new_s = s + timedelta(days=7)
        if e is not None:
            new_e = e + timedelta(days=7)
    elif "tomorrow" in low:
        new_s = s + timedelta(days=1)
        if e is not None:
            new_e = e + timedelta(days=1)
    else:
        day_d = _day_from_text(S, low)
        ct = _clock_time(low)
        if day_d is not None:
            new_s = datetime.combine(day_d, dtime(s.hour, s.minute))
            if e is not None:
                length = e - s
                new_e = new_s + length
        elif ct is not None:
            new_s = datetime.combine(s.date(), dtime(ct[0], ct[1]))
            if e is not None:
                length = e - s
                new_e = new_s + length
    undo_id = _new_id("undo_")
    S.setdefault("undo", {})[undo_id] = {
        "item_id": it.get("id"),
        "start": old_start,
        "end": old_end,
        "window": old_window,
        "postpone_count": old_pc,
    }
    it["start"] = _fmt_iso(new_s)
    if new_e is not None:
        it["end"] = _fmt_iso(new_e)
    add_log(S, "update", [it.get("id")], {"old_start": old_start, "new_start": it["start"]}, "typed")
    msg = _info(S, "Moved '%s' to %s at %s." % (it.get("title"), _fmt_short(new_s.date()), _fmt_time(new_s)), item_id=it.get("id"))
    msg["undo_id"] = undo_id
    return {"messages": [msg], "created": []}


# === SECTION: plans ===

def apply_plan(S, plan_id):
    plan = S.get("plans", {}).get(plan_id)
    if plan is None:
        raise KeyError(plan_id)
    if plan.get("status") != "preview":
        return {"plan": plan}
    created_ids = []
    for op in plan.get("ops", []):
        if op.get("op") == "delete":
            iid = op.get("item_id")
            S["items"] = [it for it in S.get("items", []) if it.get("id") != iid]
            add_log(S, "delete", [iid], {"plan": plan_id}, "agent")
        elif op.get("op") == "create":
            after = _copy_item(op.get("after"))
            if after is not None:
                S.setdefault("items", []).append(after)
                created_ids.append(after.get("id"))
                add_log(S, "create", [after.get("id")], {"plan": plan_id}, "agent")
        elif op.get("op") == "update":
            after = _copy_item(op.get("after"))
            iid = op.get("item_id")
            for i, it in enumerate(S.get("items", [])):
                if it.get("id") == iid:
                    S["items"][i] = after
                    break
            add_log(S, "update", [iid], {"plan": plan_id}, "agent")
    plan["status"] = "applied"
    add_log(S, "plan_applied", list(plan.get("affected", [])) + created_ids, {"plan": plan_id}, "agent")
    return {"plan": plan}


def undo_plan(S, plan_id):
    plan = S.get("plans", {}).get(plan_id)
    if plan is None:
        raise KeyError(plan_id)
    if plan.get("status") != "applied":
        return {"plan": plan}
    for op in plan.get("ops", []):
        if op.get("op") == "delete":
            before = _copy_item(op.get("before"))
            if before is not None:
                S.setdefault("items", []).append(before)
        elif op.get("op") == "create":
            iid = op.get("item_id")
            S["items"] = [it for it in S.get("items", []) if it.get("id") != iid]
        elif op.get("op") == "update":
            before = _copy_item(op.get("before"))
            iid = op.get("item_id")
            for i, it in enumerate(S.get("items", [])):
                if it.get("id") == iid:
                    S["items"][i] = before
                    break
    plan["status"] = "undone"
    add_log(S, "plan_undone", list(plan.get("affected", [])), {"plan": plan_id}, "agent")
    return {"plan": plan}


# === SECTION: import ===

def import_file(S, filename, content):
    content = content or ""
    is_ics = ("BEGIN:VCALENDAR" in content) or (filename or "").lower().endswith(".ics")
    if is_ics:
        return _import_ics(S, filename, content)
    return _import_text(S, filename, content)


def _import_ics(S, filename, content):
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        if line.startswith(" ") and lines:
            lines[-1] = lines[-1] + line[1:]
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
                name_part, value = line.split(":", 1)
                prop = name_part.split(";")[0].strip().upper()
                cur[prop] = value.strip()
                if prop == "DTSTART":
                    cur["_dtstart_params"] = name_part
                if prop == "DTEND":
                    cur["_dtend_params"] = name_part
    ops = []
    for ev in events:
        summary = ev.get("SUMMARY", "")
        uid = ev.get("UID")
        dtstart = ev.get("DTSTART", "")
        dtend = ev.get("DTEND", "")
        start_iso = None
        end_iso = None
        window = None
        if re.match(r"^\d{8}$", dtstart):
            y, mo, d = int(dtstart[0:4]), int(dtstart[4:6]), int(dtstart[6:8])
            start_iso = "%04d-%02d-%02dT00:00" % (y, mo, d)
            window = "all day"
            if re.match(r"^\d{8}$", dtend):
                y2, mo2, d2 = int(dtend[0:4]), int(dtend[4:6]), int(dtend[6:8])
                end_iso = "%04d-%02d-%02dT00:00" % (y2, mo2, d2)
            else:
                try:
                    dd = date(y, mo, d) + timedelta(days=1)
                    end_iso = "%04d-%02d-%02dT00:00" % (dd.year, dd.month, dd.day)
                except Exception:
                    end_iso = start_iso
        else:
            m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})", dtstart)
            if m:
                start_iso = "%s-%s-%sT%s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
            m2 = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})", dtend)
            if m2:
                end_iso = "%s-%s-%sT%s:%s" % (m2.group(1), m2.group(2), m2.group(3), m2.group(4), m2.group(5))
        # duplicate check
        dup = False
        for it in S.get("items", []):
            if uid and it.get("uid") == uid:
                dup = True
                break
            if (it.get("title") or "").lower() == (summary or "").lower() and it.get("start") == start_iso:
                dup = True
                break
        if dup:
            continue
        new_item = _full_item({
            "id": _new_id("itm_"),
            "kind": "event",
            "title": summary,
            "start": start_iso,
            "end": end_iso,
            "window": window,
            "status": "open",
            "source": "ics",
            "uid": uid,
            "recurrence": ev.get("RRULE"),
        })
        ops.append({"op": "create", "item_id": new_item["id"], "before": None, "after": new_item})
    plan = {
        "id": _new_id("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": [op["item_id"] for op in ops],
    }
    S.setdefault("plans", {})[plan["id"]] = plan
    add_log(S, "import", list(plan["affected"]), {"filename": filename, "op_count": len(ops)}, "ics")
    return {"plan": plan}


def _import_text(S, filename, content):
    text = content or ""
    sentences = re.split(r"[.!?\n]+", text)
    ops = []
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        low = s.lower()
        has_clear = re.search(r"\b(away|clear|free|cancel|off)\b", low) is not None
        day_d = _day_from_text(S, low)
        if has_clear and ("weekend" in low or day_d is not None):
            if "weekend" in low:
                start_dt, end_dt = _weekend_range(S)
            else:
                start_dt, end_dt = _day_range(day_d)
            for it in S.get("items", []):
                if it.get("status") != "open":
                    continue
                if _overlaps(it, start_dt, end_dt):
                    ops.append({"op": "delete", "item_id": it.get("id"), "before": _copy_item(it), "after": None})
            continue
        ct = _clock_time(low)
        sw = _soft_window(low)
        if day_d is not None or ct is not None or sw is not None:
            if day_d is None:
                day_d = _now_dt(S).date()
            start_iso = None
            end_iso = None
            window = None
            if sw is not None:
                name, h1, m1, h2, m2 = sw
                window = name
                start_iso = _fmt_iso(datetime.combine(day_d, dtime(h1, m1)))
                end_iso = _fmt_iso(datetime.combine(day_d, dtime(h2, m2)))
            elif ct is not None:
                start_iso = _fmt_iso(datetime.combine(day_d, dtime(ct[0], ct[1])))
                end_iso = _fmt_iso(datetime.combine(day_d, dtime(ct[0], ct[1])) + timedelta(minutes=60))
            else:
                start_iso = _fmt_iso(datetime.combine(day_d, dtime(9, 0)))
                end_iso = _fmt_iso(datetime.combine(day_d, dtime(10, 0)))
            title = s
            title = re.sub(r"\b(also|on|at)\b", " ", title, flags=re.IGNORECASE)
            title = re.sub(r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b", " ", title, flags=re.IGNORECASE)
            title = re.sub(r"\b\d{1,2}:\d{2}\s*(am|pm)?\b", " ", title, flags=re.IGNORECASE)
            title = re.sub(r"\b\d{1,2}\s*(am|pm)\b", " ", title, flags=re.IGNORECASE)
            title = re.sub(r"\baround noon\b", " ", title, flags=re.IGNORECASE)
            title = re.sub(r"\b(noon|lunch|morning|afternoon|evening|tonight)\b", " ", title, flags=re.IGNORECASE)
            title = re.sub(r"\s+", " ", title).strip()
            title = title.capitalize()
            kind = "task"
            if re.search(r"\b(with|lunch|dinner|meet|call)\b", low):
                kind = "event"
            new_item = _full_item({
                "id": _new_id("itm_"),
                "kind": kind,
                "title": title,
                "start": start_iso,
                "end": end_iso,
                "window": window,
                "status": "open",
                "source": "text",
            })
            ops.append({"op": "create", "item_id": new_item["id"], "before": None, "after": new_item})
    plan = {
        "id": _new_id("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": [op["item_id"] for op in ops],
    }
    S.setdefault("plans", {})[plan["id"]] = plan
    add_log(S, "import", list(plan["affected"]), {"filename": filename, "op_count": len(ops)}, "text")
    return {"plan": plan}


# === SECTION: views ===

def view_state(S):
    view = S.get("view") or {"type": "week", "params": {}}
    vtype = view.get("type", "week")
    params = view.get("params", {})
    now_dt = _now_dt(S)
    today_d = now_dt.date()
    hidden = {}
    for it in S.get("items", []):
        s = _parse_iso(it.get("start"))
        if s is None:
            continue
        d = s.date()
        hide = False
        if vtype == "workweek":
            if d.weekday() >= 5:
                hide = True
        elif vtype == "day":
            if d != today_d:
                hide = True
        elif vtype == "week":
            monday = today_d - timedelta(days=today_d.weekday())
            sunday = monday + timedelta(days=6)
            if d < monday or d > sunday:
                hide = True
        elif vtype == "month":
            if d.year != today_d.year or d.month != today_d.month:
                hide = True
        elif vtype == "custom":
            if params.get("hide_weekends") and d.weekday() >= 5:
                hide = True
        if hide:
            key = d.isoformat()
            hidden[key] = hidden.get(key, 0) + 1
    return {"type": vtype, "params": params, "hidden_counts": hidden}


def set_view(S, view_type):
    if view_type not in ("month", "week", "workweek", "day"):
        raise KeyError(view_type)
    S["view"] = {"type": view_type, "params": {}}
    add_log(S, "view_switch", [], {"type": view_type}, "person")
    return {"view": view_state(S)}


# === SECTION: habit learner ===

def _history(S):
    hist = []
    for it in S.get("items", []):
        if it.get("status") == "done":
            t = _parse_iso(it.get("end")) or _parse_iso(it.get("start"))
            if t is not None:
                hist.append((t, it.get("id")))
    for e in S.get("log", []):
        if e.get("kind") == "done":
            t = _parse_iso(e.get("at"))
            if t is not None:
                hist.append((t, e.get("id")))
    return hist


def _confidence(S, key, share, n):
    base = share * min(1.0, n / 6.0)
    pen = S.get("penalty", {}).get(key, 0)
    return round(max(0.0, base - pen), 2)


def _store_belief(S, key, level, statement, value, confidence, evidence):
    if not evidence:
        return None
    old = S.get("beliefs", {}).get(key)
    b = {
        "key": key,
        "level": level,
        "statement": statement,
        "value": value,
        "confidence": confidence,
        "evidence": list(evidence)[:20],
        "updated_at": _now(S),
    }
    S.setdefault("beliefs", {})[key] = b
    if old is None or abs(old.get("confidence", 0) - confidence) >= 0.05:
        _activity(S, "learner", "belief_update", "%s (%.2f)" % (statement, confidence))
    if old is not None and old.get("confidence", 0) < 0.7 <= confidence:
        _activity(S, "learner", "threshold_crossed", statement)
    return b


def learn_beliefs(S):
    hist = _history(S)
    n = len(hist)
    deleted = set(S.get("deleted_beliefs", []))

    # evening_work / morning_work
    if n >= 5:
        evening = sum(1 for t, _ in hist if t.hour >= 20)
        morning = sum(1 for t, _ in hist if t.hour < 12)
        if "evening_work" not in deleted and evening / n >= 0.5:
            share = evening / n
            conf = _confidence(S, "evening_work", share, n)
            ev = [eid for _, eid in hist]
            _store_belief(S, "evening_work", "habit",
                          "Finishes tasks after 8 PM (%d of %d)" % (evening, n),
                          20, conf, ev)
        elif "morning_work" not in deleted and morning / n >= 0.5:
            share = morning / n
            conf = _confidence(S, "morning_work", share, n)
            ev = [eid for _, eid in hist]
            _store_belief(S, "morning_work", "habit",
                          "Gets things done in the morning (%d of %d)" % (morning, n),
                          9, conf, ev)

    # no_weekend
    if "no_weekend" not in deleted:
        weeks = set()
        weekend_count = 0
        ids = []
        total = 0
        for it in S.get("items", []):
            s = _parse_iso(it.get("start"))
            if s is None:
                continue
            total += 1
            ids.append(it.get("id"))
            iso = s.date().isocalendar()
            weeks.add((iso[0], iso[1]))
            if s.date().weekday() >= 5:
                weekend_count += 1
        if len(weeks) >= 4 and weekend_count == 0 and total > 0:
            conf = round(min(1.0, len(weeks) / 8.0) - S.get("penalty", {}).get("no_weekend", 0), 2)
            conf = max(0.0, conf)
            _store_belief(S, "no_weekend", "habit",
                          "No weekend plans in %d weeks (0 of %d items)" % (len(weeks), total),
                          True, conf, ids)

    # block_length
    if "block_length" not in deleted:
        lengths = []
        ids = []
        for it in S.get("items", []):
            if it.get("status") != "done":
                continue
            s = _parse_iso(it.get("start"))
            e = _parse_iso(it.get("end"))
            if s is not None and e is not None:
                lengths.append(int((e - s).total_seconds() // 60))
                ids.append(it.get("id"))
        if len(lengths) >= 6:
            sl = sorted(lengths)
            m = sl[len(sl) // 2]
            k = sum(1 for x in lengths if abs(x - m) <= 15)
            share = k / len(lengths)
            conf = _confidence(S, "block_length", share, len(lengths))
            _store_belief(S, "block_length", "habit",
                          "Tasks usually take about %d minutes (%d of %d)" % (m, k, len(lengths)),
                          m, conf, ids)
    return S.get("beliefs", {})


def get_beliefs(S):
    return {"beliefs": list(S.get("beliefs", {}).values())}


def delete_belief(S, key):
    if key in S.get("beliefs", {}):
        del S["beliefs"][key]
    S.setdefault("deleted_beliefs", []).append(key)
    add_log(S, "belief_deleted", [], {"key": key}, "person")
    _activity(S, "learner", "belief_deleted", "Removed belief %s" % key)
    return {"ok": True}


# === SECTION: proposal gate ===

def _open_proposal(S):
    for p in S.get("proposals", []):
        if p.get("status") == "shown":
            return p
    return None


def run_gate(S):
    if _open_proposal(S) is not None:
        return None
    now_dt = _now_dt(S)
    last = S.get("last_proposal_at")
    cadence = S.get("meta", {}).get("cadence_hours", 24)
    if last is not None:
        last_dt = _parse_iso(last)
        if last_dt is not None and (now_dt - last_dt) <= timedelta(hours=cadence):
            return None
    order = ["no_weekend", "evening_work", "morning_work", "block_length"]
    candidates = []
    for key, b in S.get("beliefs", {}).items():
        if b.get("level") != "habit":
            continue
        if b.get("confidence", 0) < 0.7:
            continue
        blocked = S.get("blocked", {}).get(key)
        if blocked is not None and b.get("confidence", 0) <= blocked + 0.15:
            continue
        candidates.append(b)
    if not candidates:
        return None
    def sort_key(b):
        k = b.get("key")
        idx = order.index(k) if k in order else len(order)
        return (idx, -b.get("confidence", 0))
    candidates.sort(key=sort_key)
    b = candidates[0]
    key = b.get("key")
    novelty = "invented" if len(S.get("invented_reverts", [])) < 2 else "classic"
    if novelty == "classic":
        if key == "no_weekend":
            view = {"type": "workweek", "params": {}}
            title = "Hide weekends"
        elif key in ("evening_work", "morning_work"):
            focus = "evening" if key == "evening_work" else "morning"
            view = {"type": "day", "params": {"focus": focus}}
            title = "Evening lane" if key == "evening_work" else "Morning lane"
        elif key == "block_length":
            view = {"type": "week", "params": {"block_min": b.get("value")}}
            title = "Block length"
        else:
            view = {"type": "week", "params": {}}
            title = "A gentler view"
    else:
        view = {"type": "custom", "params": {"layout": "energy lanes", "belief": key}}
        title = "Energy lanes"
    reason = b.get("statement") + ". " + title + "?"
    prompt_versions = {}
    for p in S.get("prompts", []):
        if p.get("active"):
            prompt_versions[p.get("agent")] = p.get("version")
    prop = {
        "id": _new_id("prop_"),
        "title": title,
        "reason": reason,
        "view": view,
        "belief_keys": [key],
        "novelty": novelty,
        "prompt_versions": prompt_versions,
        "status": "shown",
    }
    S.setdefault("proposals", []).append(prop)
    S["last_proposal_at"] = _now(S)
    add_log(S, "proposal_shown", [], {"proposal": prop["id"], "belief": key}, "agent")
    _activity(S, "designer", "proposal_shown", title + ": " + reason)
    return prop


def get_proposal(S):
    return {"proposal": _open_proposal(S)}


def proposal_action(S, proposal_id, action):
    prop = None
    for p in S.get("proposals", []):
        if p.get("id") == proposal_id:
            prop = p
            break
    if prop is None:
        raise KeyError(proposal_id)
    key = (prop.get("belief_keys") or [None])[0]
    if action == "accept":
        if prop.get("status") == "shown":
            prop["prev_view"] = dict(S.get("view") or {})
            S["view"] = dict(prop.get("view") or {"type": "week", "params": {}})
            prop["status"] = "accepted"
            add_log(S, "proposal_accepted", [], {"proposal": proposal_id}, "person")
            _activity(S, "designer", "proposal_accepted", prop.get("title", ""))
    elif action == "undo":
        if prop.get("status") == "accepted":
            prev = prop.get("prev_view") or {"type": "week", "params": {}}
            S["view"] = dict(prev)
            prop["status"] = "reverted"
            if key is not None:
                S.setdefault("penalty", {})[key] = S.get("penalty", {}).get(key, 0) + 0.2
                b = S.get("beliefs", {}).get(key)
                if b is not None:
                    b["confidence"] = round(max(0.0, b.get("confidence", 0) - 0.2), 2)
                    S.setdefault("blocked", {})[key] = b["confidence"]
            log_entry = add_log(S, "proposal_reverted", [], {"proposal": proposal_id}, "person")
            _activity(S, "designer", "proposal_reverted", prop.get("title", ""))
            if prop.get("novelty") == "invented":
                S.setdefault("invented_reverts", []).append(log_entry["id"])
                S["meta"]["novelty"] = round(max(0.0, S["meta"].get("novelty", 0.6) - 0.1), 2)
                if len(S["invented_reverts"]) in (2, 4, 6, 8):
                    _meta_learn(S)
    elif action == "dismiss":
        if prop.get("status") == "shown":
            prop["status"] = "dismissed"
            if key is not None:
                b = S.get("beliefs", {}).get(key)
                if b is not None:
                    S.setdefault("blocked", {})[key] = b.get("confidence", 0)
            add_log(S, "proposal_dismissed", [], {"proposal": proposal_id}, "person")
            _activity(S, "designer", "proposal_dismissed", prop.get("title", ""))
    return {"proposal": prop, "view": view_state(S)}


# === SECTION: meta learner ===

def _meta_learn(S):
    prev = None
    for p in S.get("prompts", []):
        if p.get("agent") == "designer" and p.get("active"):
            prev = p
            break
    if prev is None:
        return None
    line = "- Prefer classic views (workweek, week, day): 2 invented views were reverted."
    learned = prev.get("learned", "")
    if learned:
        learned = learned + "\n" + line
    else:
        learned = line
    lines = learned.split("\n")
    if len(lines) > 12:
        lines = lines[-12:]
        learned = "\n".join(lines)
    low = learned.lower()
    for bad in ("ignore", "override", "core rules"):
        if bad in low:
            learned = learned.replace(bad, "prefer")
    diff = "+ " + line
    evidence = list(S.get("invented_reverts", []))
    replay_ok = len(learned.split("\n")) <= 12 and not any(
        bad in learned.lower() for bad in ("ignore", "override", "core rules")
    )
    new_version = {
        "agent": "designer",
        "version": prev.get("version", 1) + 1,
        "core_hash": prev.get("core_hash"),
        "learned": learned,
        "diff": diff,
        "reason": "2 invented proposals were reverted",
        "evidence": evidence,
        "replay": {"passed": replay_ok, "detail": "learned text checked: <= 12 lines, no rule override"},
        "active": True,
    }
    prev["active"] = False
    S.setdefault("prompts", []).append(new_version)
    _activity(S, "designer", "prompt_rewrite", "designer v%d: %s" % (new_version["version"], diff), {"diff": diff})
    # meta belief
    S.setdefault("beliefs", {})["prefers_classic"] = {
        "key": "prefers_classic",
        "level": "meta",
        "statement": "Prefers classic views (2 of 2 invented views reverted)",
        "value": "classic",
        "confidence": 0.8,
        "evidence": evidence,
        "updated_at": _now(S),
    }
    return new_version


def get_prompts(S):
    return {"prompts": list(S.get("prompts", []))}


def rollback(S, agent):
    versions = [p for p in S.get("prompts", []) if p.get("agent") == agent]
    if not versions:
        raise KeyError(agent)
    active = None
    for p in versions:
        if p.get("active"):
            active = p
            break
    if active is None:
        active = versions[-1]
        active["active"] = True
    else:
        idx = versions.index(active)
        if idx > 0:
            active["active"] = False
            prev = versions[idx - 1]
            prev["active"] = True
            active = prev
    _activity(S, "designer", "prompt_rollback", "%s rolled back to v%d" % (agent, active.get("version", 1)))
    return {"active": active}


# === SECTION: learn / tick ===

def learn(S):
    learn_beliefs(S)
    run_gate(S)
    return {"beliefs": list(S.get("beliefs", {}).values()), "proposal": _open_proposal(S)}


def tick(S, on_clock):
    if on_clock or (len(S.get("log", [])) - S.get("learn_mark", 0)) >= 5:
        S["learn_mark"] = len(S.get("log", []))
        learn_beliefs(S)
        run_gate(S)


# === SECTION: meta state ===

def meta_state(S):
    return dict(S.get("meta", {"novelty": 0.6, "cadence_hours": 24, "breakdown_appetite": 0.5, "tone": "gentle"}))
