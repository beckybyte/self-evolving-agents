import json
import re
import uuid
from datetime import datetime, date, time as dtime, timedelta

import v2

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot",
          "lazy", "urgent", "alert"]

STOP_WORDS = set("""move reschedule push shift postpone bump delay put off back by later
earlier cancel delete remove drop scrap skip my the a an to on at for next week weeks day
days one please it""".split())

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

CLEAR_PHRASES = ["empty", "clear", "clear out", "free up", "wipe", "nothing on",
                 "no plans", "keep", "block out", "cancel everything", "cancel all",
                 "make room"]

CANCEL_STARTS = ["cancel", "delete", "remove", "drop", "scrap", "skip", "get rid of",
                 "forget", "i don't need", "i dont need"]

MOVE_STARTS = ["move", "reschedule", "push", "shift", "postpone", "bump", "delay",
               "put off", "pull"]

QUERY_STARTS = ["what", "what's", "whats", "anything", "any", "show", "when",
                "do i have", "is there", "am i"]

PART_OF_DAY = {
    "morning": (0, 12),
    "afternoon": (12, 17),
    "evening": (17, 24),
    "tonight": (17, 24),
}

SOFT_WINDOWS = {
    "around noon": (12, 0, 13, 0),
    "noon": (12, 0, 13, 0),
    "lunch": (12, 0, 13, 0),
    "morning": (9, 0, 12, 0),
    "afternoon": (14, 0, 17, 0),
    "this afternoon": (14, 0, 17, 0),
    "evening": (18, 0, 21, 0),
    "tonight": (20, 0, 22, 0),
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now_dt(S):
    try:
        return datetime.strptime(S["now"], "%Y-%m-%dT%H:%M")
    except Exception:
        return datetime(2026, 10, 19, 20, 0)


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


def _clean(text):
    if not text:
        return text
    out = text
    low = out.lower()
    for bad in BANNED:
        if bad in low:
            out = re.sub(re.escape(bad), "moved", out, flags=re.IGNORECASE)
            low = out.lower()
    return out


def _msg(S, kind, text, item_id=None, undo_id=None):
    m = {
        "id": _new_id("msg_"),
        "role": "agent",
        "kind": kind,
        "text": _clean(text),
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": undo_id,
    }
    S.setdefault("messages", []).append(m)
    return m


def _default_item():
    return {
        "id": None, "kind": "task", "title": "", "start": None, "end": None,
        "window": None, "status": "open", "why": None, "parent_id": None,
        "estimate_min": None, "postpone_count": 0, "source": "typed",
    }


def _full_item(partial):
    it = _default_item()
    it.update(partial or {})
    return it


def _log(S, kind, item_ids, payload, source):
    try:
        v2.add_log(S, kind, item_ids, payload, source)
    except Exception:
        pass


def _find_item(S, item_id):
    for it in S.get("items", []):
        if it.get("id") == item_id:
            return it
    return None


def _item_index(S, item_id):
    for i, it in enumerate(S.get("items", [])):
        if it.get("id") == item_id:
            return i
    return -1


def _copy_item(it):
    return json.loads(json.dumps(it))


# ---------------------------------------------------------------------------
# day words
# ---------------------------------------------------------------------------

def _soonest_weekday(today_d, wd):
    delta = (wd - today_d.weekday()) % 7
    if delta == 0:
        delta = 7
    return today_d + timedelta(days=delta)


def _find_day(text, now_dt):
    """Return (date, matched_phrase) or (None, None)."""
    today_d = now_dt.date()
    low = text.lower()

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, m.group(0)
        except Exception:
            pass

    m = re.search(r"\b(" + "|".join(MONTHS.keys()) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
    if m:
        mon = MONTHS[m.group(1)]
        day = int(m.group(2))
        year = today_d.year
        try:
            d = date(year, mon, day)
        except Exception:
            d = None
        if d is not None:
            if d < today_d:
                try:
                    d = date(year + 1, mon, day)
                except Exception:
                    pass
            return d, m.group(0)

    if re.search(r"\btoday\b", low):
        return today_d, "today"
    if re.search(r"\btomorrow\b", low):
        return today_d + timedelta(days=1), "tomorrow"

    for name, wd in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            return _soonest_weekday(today_d, wd), name

    m = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", low)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = date(today_d.year, today_d.month, day)
            except Exception:
                d = None
            if d is not None and d < today_d:
                y = today_d.year + (1 if today_d.month == 12 else 0)
                mo = 1 if today_d.month == 12 else today_d.month + 1
                try:
                    d = date(y, mo, day)
                except Exception:
                    d = None
            if d is not None:
                return d, m.group(0)

    return None, None


def _find_part_of_day(text):
    low = text.lower()
    for name in ("morning", "afternoon", "evening", "tonight"):
        if re.search(r"\b" + name + r"\b", low):
            return name
    return None


def _find_clock(text):
    """Return (hour, minute) or None."""
    low = text.lower()
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        ap = m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1))
        ap = m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return h, 0
    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        if 0 <= h <= 23:
            return h, 0
    return None


def _find_soft_window(text):
    low = text.lower()
    for key in ("around noon", "this afternoon", "noon", "lunch", "morning",
                "afternoon", "evening", "tonight"):
        if key in low:
            return key
    return None


# ---------------------------------------------------------------------------
# item matching
# ---------------------------------------------------------------------------

def _match_words(text):
    words = re.findall(r"[a-z0-9']+", text.lower())
    return set(w for w in words if w not in STOP_WORDS)


def _best_match(S, text):
    wanted = _match_words(text)
    best = None
    best_score = 0
    best_start = None
    for it in S.get("items", []):
        if it.get("status") != "open":
            continue
        title_words = set(w for w in re.findall(r"[a-z0-9']+", (it.get("title") or "").lower())
                          if len(w) >= 3)
        score = len(title_words & wanted)
        if score <= 0:
            continue
        st = _parse_iso(it.get("start"))
        if best is None or score > best_score or (
                score == best_score and st is not None and
                (best_start is None or st < best_start)):
            best = it
            best_score = score
            best_start = st
    return best


# ---------------------------------------------------------------------------
# ranges
# ---------------------------------------------------------------------------

def _weekend_range(now_dt):
    today_d = now_dt.date()
    wd = today_d.weekday()
    if wd == 5:
        sat = today_d
    elif wd == 6:
        sat = today_d - timedelta(days=1)
    else:
        sat = today_d + timedelta(days=(5 - wd) % 7)
    start = datetime.combine(sat, dtime(0, 0))
    end = datetime.combine(sat + timedelta(days=2), dtime(0, 0))
    return start, end


def _range_for(S, text):
    """Return (start_dt, end_dt, label) or None."""
    now_dt = _now_dt(S)
    low = text.lower()
    part = _find_part_of_day(text)
    if re.search(r"\bweekend\b", low):
        start, end = _weekend_range(now_dt)
        label = "weekend"
        if part:
            ph0, ph1 = PART_OF_DAY[part]
            start = datetime.combine(start.date(), dtime(ph0, 0))
            end = datetime.combine(start.date(), dtime(min(ph1, 23), 59))
            label = "weekend " + part
        return start, end, label
    d, _phrase = _find_day(text, now_dt)
    if d is None:
        return None
    if part:
        ph0, ph1 = PART_OF_DAY[part]
        start = datetime.combine(d, dtime(ph0, 0))
        end = datetime.combine(d, dtime(min(ph1, 23), 59))
        return start, end, part
    start = datetime.combine(d, dtime(0, 0))
    end = datetime.combine(d + timedelta(days=1), dtime(0, 0))
    return start, end, None


def _overlaps(S, start, end):
    out = []
    for it in S.get("items", []):
        if it.get("status") != "open":
            continue
        st = _parse_iso(it.get("start"))
        if st is None:
            continue
        en = _parse_iso(it.get("end"))
        if en is None:
            en = st + timedelta(minutes=60)
        if st < end and en > start:
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# purpose extraction
# ---------------------------------------------------------------------------

def _purpose(text):
    low = text.lower()
    m = re.search(r"\bfor\s+(.+)$", low)
    if m:
        p = m.group(1)
    else:
        m = re.search(r"[,\u2014-]\s*(.+)$", text)
        if m:
            p = m.group(1)
        else:
            m = re.search(r"\b(?:because|since|as)\s+(.+)$", low)
            if m:
                p = m.group(1)
            else:
                p = ""
    p = p.strip().strip(".!?")
    for lead in ["i have", "i've got", "i got", "i'm", "i am", "we have", "we're",
                 "there's", "a", "an", "my", "some", "please"]:
        if p.lower().startswith(lead + " "):
            p = p[len(lead):].strip()
        elif p.lower() == lead:
            p = ""
    if not p:
        return "Time off"
    return p[0].upper() + p[1:]


# ---------------------------------------------------------------------------
# BULK
# ---------------------------------------------------------------------------

def _handle_bulk(S, text):
    low = text.lower()
    has_clear = any(p in low for p in CLEAR_PHRASES)
    if not has_clear:
        return None
    rng = _range_for(S, text)
    if rng is None:
        return None
    start, end, label = rng
    affected = _overlaps(S, start, end)
    purpose = _purpose(text)

    ops = []
    for it in affected:
        ops.append({"op": "delete", "item_id": it["id"],
                    "before": _copy_item(it), "after": None})

    if re.search(r"\bweekend\b", low):
        sat = start.date()
        if sat.weekday() != 5:
            sat = sat + timedelta(days=(5 - sat.weekday()) % 7)
        c_start = datetime.combine(sat, dtime(9, 0))
        c_end = datetime.combine(sat + timedelta(days=1), dtime(18, 0))
        window = "weekend"
    else:
        d = start.date()
        if label in PART_OF_DAY:
            ph0, ph1 = PART_OF_DAY[label]
            c_start = datetime.combine(d, dtime(ph0, 0))
            c_end = datetime.combine(d, dtime(min(ph1, 23), 0))
            window = label
        else:
            c_start = datetime.combine(d, dtime(9, 0))
            c_end = datetime.combine(d, dtime(18, 0))
            window = d.strftime("%A").lower()

    new_item = _full_item({
        "id": _new_id("itm_"),
        "kind": "event",
        "title": purpose,
        "start": _fmt_iso(c_start),
        "end": _fmt_iso(c_end),
        "window": window,
        "source": "typed",
    })
    ops.append({"op": "create", "item_id": None, "before": None, "after": new_item})

    plan = {
        "id": _new_id("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": ops,
        "affected": [it["id"] for it in affected],
    }
    S.setdefault("plans", {})[plan["id"]] = plan

    n = len(affected)
    text_out = ("I can clear %d item%s and add '%s'. Shall I apply it?"
                % (n, "" if n == 1 else "s", purpose))
    msg = _msg(S, "info", text_out)
    return {"messages": [msg], "created": [], "plan": plan}


# ---------------------------------------------------------------------------
# QUERY
# ---------------------------------------------------------------------------

def _handle_query(S, text):
    low = text.strip().lower()
    is_query = low.endswith("?") or any(
        low.startswith(p + " ") or low == p for p in QUERY_STARTS)
    if not is_query:
        return None
    now_dt = _now_dt(S)
    d, _phrase = _find_day(text, now_dt)
    if d is None:
        d = now_dt.date()
    day_name = d.strftime("%A")
    found = []
    for it in S.get("items", []):
        st = _parse_iso(it.get("start"))
        if st is not None and st.date() == d:
            found.append((st, it))
    found.sort(key=lambda x: x[0])
    if not found:
        text_out = "%s looks free." % day_name
    else:
        parts = []
        for st, it in found:
            title = (it.get("title") or "").strip()
            if it.get("window"):
                parts.append("%s %s" % (title, it["window"]))
            else:
                parts.append("%s at %s" % (title, st.strftime("%H:%M")))
        text_out = "On %s: %s." % (day_name, ", ".join(parts))
    msg = _msg(S, "reply", text_out)
    _log(S, "query", [it["id"] for _st, it in found],
         {"text": text, "day": d.isoformat()}, "typed")
    return {"messages": [msg], "created": []}


# ---------------------------------------------------------------------------
# CANCEL
# ---------------------------------------------------------------------------

def _handle_cancel(S, text):
    low = text.strip().lower()
    if not any(low.startswith(p) for p in CANCEL_STARTS):
        return None
    it = _best_match(S, text)
    if it is None:
        msg = _msg(S, "info", "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    plan = {
        "id": _new_id("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": [{"op": "delete", "item_id": it["id"],
                 "before": _copy_item(it), "after": None}],
        "affected": [it["id"]],
    }
    S.setdefault("plans", {})[plan["id"]] = plan
    msg = _msg(S, "info", "I can remove '%s' for you. Apply the change?" % it.get("title"))
    return {"messages": [msg], "created": [], "plan": plan}


# ---------------------------------------------------------------------------
# MOVE
# ---------------------------------------------------------------------------

def _shift_for(S, text, item):
    """Return (new_start_dt, new_end_dt) or None."""
    low = text.lower()
    st = _parse_iso(item.get("start"))
    en = _parse_iso(item.get("end"))
    if st is None:
        return None
    length = (en - st) if en is not None else timedelta(minutes=60)

    # explicit "N weeks" / "N days"
    m = re.search(r"\b(\d+)\s+weeks?\b", low)
    if m:
        ns = st + timedelta(days=7 * int(m.group(1)))
        return ns, ns + length
    m = re.search(r"\b(\d+)\s+days?\b", low)
    if m:
        ns = st + timedelta(days=int(m.group(1)))
        return ns, ns + length

    # natural-language relative phrases: "back a week", "push ... back a week",
    # "a week later", "next week", "one week", "back a day", "a day later", ...
    if re.search(r"\b(?:back|forward|ahead|out|by|in|for|another|one|a|an|next|"
                 r"following|coming)\s+(?:a\s+|an\s+|one\s+|two\s+|three\s+)?"
                 r"(?:week|weeks|day|days|month|months)\b", low) or \
            re.search(r"\b(?:a|an|one|two|three)\s+(?:week|weeks|day|days|month|months)\s+"
                      r"(?:later|from now|out|ahead|forward)\b", low) or \
            re.search(r"\b(?:next|following|coming)\s+(?:week|day|month)\b", low):
        # figure out the unit and count
        unit_days = 7
        count = 1
        mu = re.search(r"\b(two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
                       r"(week|weeks|day|days|month|months)\b", low)
        if mu:
            word = mu.group(1)
            num_map = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                       "seven": 7, "eight": 8, "nine": 9, "ten": 10}
            count = num_map.get(word, None)
            if count is None:
                try:
                    count = int(word)
                except Exception:
                    count = 1
            unit = mu.group(2)
            if unit.startswith("day"):
                unit_days = 1
            elif unit.startswith("month"):
                unit_days = 30
            else:
                unit_days = 7
        else:
            if re.search(r"\b(?:day|days)\b", low):
                unit_days = 1
            elif re.search(r"\b(?:month|months)\b", low):
                unit_days = 30
            else:
                unit_days = 7
        ns = st + timedelta(days=unit_days * count)
        return ns, ns + length

    # "tomorrow" / "a day" style
    if re.search(r"\b(tomorrow|a day|back a day|one day)\b", low):
        ns = st + timedelta(days=1)
        return ns, ns + length

    now_dt = _now_dt(S)
    for name, wd in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            target = _soonest_weekday(now_dt.date(), wd)
            ns = datetime.combine(target, st.time())
            return ns, ns + length

    clock = _find_clock(text)
    if clock is not None:
        h, mi = clock
        ns = datetime.combine(st.date(), dtime(h, mi))
        return ns, ns + length

    return None


def _handle_move(S, text):
    low = text.strip().lower()
    if not any(low.startswith(p) for p in MOVE_STARTS):
        return None
    it = _best_match(S, text)
    if it is None:
        msg = _msg(S, "info", "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    shift = _shift_for(S, text, it)
    if shift is None:
        msg = _msg(S, "info", "I couldn't find that one. Which item do you mean?")
        return {"messages": [msg], "created": []}
    ns, ne = shift
    undo_id = _new_id("u_")
    S.setdefault("undo", {})[undo_id] = {
        "item_id": it["id"],
        "before": _copy_item(it),
        "start": it.get("start"),
        "end": it.get("end"),
        "window": it.get("window"),
        "postpone_count": it.get("postpone_count", 0),
    }
    old_start = it.get("start")
    it["start"] = _fmt_iso(ns)
    it["end"] = _fmt_iso(ne)
    _log(S, "update", [it["id"]],
         {"old_start": old_start, "new_start": it["start"]}, "typed")
    text_out = "Moved '%s' to %s." % (it.get("title"), ns.strftime("%a %b %d at %H:%M"))
    msg = _msg(S, "info", text_out, item_id=it["id"], undo_id=undo_id)
    return {"messages": [msg], "created": []}


# ---------------------------------------------------------------------------
# handle_command
# ---------------------------------------------------------------------------

def handle_command(S, text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        r = _handle_bulk(S, text)
        if r is not None:
            return r
        r = _handle_query(S, text)
        if r is not None:
            return r
        r = _handle_cancel(S, text)
        if r is not None:
            return r
        r = _handle_move(S, text)
        if r is not None:
            return r
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------

def apply_plan(S, plan_id):
    plans = S.setdefault("plans", {})
    if plan_id not in plans:
        raise KeyError(plan_id)
    plan = plans[plan_id]
    if plan.get("status") != "preview":
        return {"plan": plan}

    items = S.setdefault("items", [])
    created_ids = []
    for op in plan.get("ops", []):
        kind = op.get("op")
        if kind == "delete":
            idx = _item_index(S, op.get("item_id"))
            if idx >= 0:
                items.pop(idx)
            _log(S, "delete", [op.get("item_id")], {"plan": plan_id}, "agent")
        elif kind == "create":
            after = _copy_item(op.get("after") or {})
            if not after.get("id"):
                after["id"] = _new_id("itm_")
            items.append(after)
            created_ids.append(after["id"])
            _log(S, "create", [after["id"]], {"plan": plan_id}, "agent")
        elif kind == "update":
            idx = _item_index(S, op.get("item_id"))
            after = _copy_item(op.get("after") or {})
            if idx >= 0:
                items[idx] = after
            else:
                items.append(after)
            _log(S, "update", [op.get("item_id")], {"plan": plan_id}, "agent")

    plan["status"] = "applied"
    _log(S, "plan_applied", list(plan.get("affected", [])) + created_ids,
         {"plan": plan_id}, "agent")
    return {"plan": plan}


def undo_plan(S, plan_id):
    plans = S.setdefault("plans", {})
    if plan_id not in plans:
        raise KeyError(plan_id)
    plan = plans[plan_id]
    if plan.get("status") != "applied":
        return {"plan": plan}

    items = S.setdefault("items", [])
    # remove created items
    for op in plan.get("ops", []):
        if op.get("op") == "create":
            after = op.get("after") or {}
            aid = after.get("id")
            if aid:
                idx = _item_index(S, aid)
                if idx >= 0:
                    items.pop(idx)
    # restore deleted / updated items
    for op in plan.get("ops", []):
        if op.get("op") in ("delete", "update"):
            before = op.get("before")
            if not before:
                continue
            bid = before.get("id")
            idx = _item_index(S, bid)
            if idx >= 0:
                items[idx] = _copy_item(before)
            else:
                items.append(_copy_item(before))

    plan["status"] = "undone"
    _log(S, "plan_undone", list(plan.get("affected", [])), {"plan": plan_id}, "agent")
    return {"plan": plan}


# ---------------------------------------------------------------------------
# ICS import
# ---------------------------------------------------------------------------

def _unfold(content):
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")
    out = []
    for line in lines:
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
            else:
                out.append(line[1:])
        else:
            out.append(line)
    return out


def _parse_ics_dt(value):
    """Return (iso_string, is_date_only) or (None, False)."""
    v = value.strip()
    if not v:
        return None, False
    if re.match(r"^\d{8}$", v):
        try:
            d = datetime.strptime(v, "%Y%m%d")
            return d.strftime("%Y-%m-%dT00:00"), True
        except Exception:
            return None, False
    m = re.match(r"^(\d{8})T(\d{4})(\d{2})?Z?$", v)
    if m:
        try:
            d = datetime.strptime(m.group(1) + "T" + m.group(2), "%Y%m%dT%H%M")
            return d.strftime("%Y-%m-%dT%H:%M"), False
        except Exception:
            return None, False
    return None, False


def _import_ics(S, filename, content):
    lines = _unfold(content)
    events = []
    cur = None
    for line in lines:
        stripped = line.strip()
        if stripped.upper() == "BEGIN:VEVENT":
            cur = {}
            continue
        if stripped.upper() == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name = head.split(";", 1)[0].strip().upper()
        cur[name] = value.strip()

    existing_uids = set()
    existing_title_start = set()
    for it in S.get("items", []):
        if it.get("uid"):
            existing_uids.add(it["uid"])
        existing_title_start.add(((it.get("title") or "").lower(), it.get("start")))

    ops = []
    for ev in events:
        uid = ev.get("UID")
        summary = ev.get("SUMMARY") or "Untitled"
        ds_raw = ev.get("DTSTART")
        de_raw = ev.get("DTEND")
        start_iso, date_only = _parse_ics_dt(ds_raw) if ds_raw else (None, False)
        end_iso, _ = _parse_ics_dt(de_raw) if de_raw else (None, False)
        window = None
        if date_only:
            window = "all day"
            if end_iso is None and start_iso:
                sd = _parse_iso(start_iso)
                if sd is not None:
                    end_iso = _fmt_iso(sd + timedelta(days=1))
        if uid and uid in existing_uids:
            continue
        if (summary.lower(), start_iso) in existing_title_start:
            continue
        item = _full_item({
            "id": _new_id("itm_"),
            "kind": "event",
            "title": summary,
            "start": start_iso,
            "end": end_iso,
            "window": window,
            "source": "ics",
        })
        item["uid"] = uid
        item["recurrence"] = ev.get("RRULE")
        ops.append({"op": "create", "item_id": None, "before": None, "after": item})
        if uid:
            existing_uids.add(uid)
        existing_title_start.add((summary.lower(), start_iso))

    plan = {
        "id": _new_id("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": [],
    }
    S.setdefault("plans", {})[plan["id"]] = plan
    _log(S, "import", [], {"filename": filename, "ops": len(ops)}, "ics")
    return {"plan": plan}


# ---------------------------------------------------------------------------
# TEXT import
# ---------------------------------------------------------------------------

def _strip_words(text, words):
    out = text
    for w in words:
        out = re.sub(r"\b" + re.escape(w) + r"\b", " ", out, flags=re.IGNORECASE)
    return out


def _text_create(S, sentence):
    now_dt = _now_dt(S)
    d, _phrase = _find_day(sentence, now_dt)
    clock = _find_clock(sentence)
    soft = _find_soft_window(sentence)
    if d is None and clock is None and soft is None:
        return None
    if d is None:
        d = now_dt.date()

    window = None
    if clock is not None:
        h, mi = clock
        start = datetime.combine(d, dtime(h, mi))
        end = start + timedelta(minutes=60)
    elif soft is not None:
        window = soft
        h0, m0, h1, m1 = SOFT_WINDOWS.get(soft, (9, 0, 10, 0))
        start = datetime.combine(d, dtime(h0, m0))
        end = datetime.combine(d, dtime(h1, m1))
    else:
        start = datetime.combine(d, dtime(9, 0))
        end = datetime.combine(d, dtime(10, 0))

    low = sentence.lower()
    is_event = any(w in low for w in ["with", "lunch", "dinner", "meet", "call"])

    title = sentence
    title = re.sub(r"\b(also|please)\b", " ", title, flags=re.IGNORECASE)
    title = _strip_words(title, list(WEEKDAYS.keys()) + list(MONTHS.keys()))
    title = re.sub(r"\b(today|tomorrow|this|next|on|at|around|noon|morning|afternoon|evening|tonight)\b",
                   " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", title)
    title = re.sub(r"\b\d{1,2}(:\d{2})?\s*(am|pm)?\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b\d{1,2}(st|nd|rd|th)\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" ,.;:-")
    if not title:
        title = "New item"
    title = title[0].upper() + title[1:]

    return _full_item({
        "id": _new_id("itm_"),
        "kind": "event" if is_event else "task",
        "title": title,
        "start": _fmt_iso(start),
        "end": _fmt_iso(end),
        "window": window,
        "source": "text",
    })


def _import_text(S, filename, content):
    raw = content.replace("\r\n", "\n").replace("\r", "\n")
    sentences = re.split(r"[.!?\n]+", raw)
    ops = []
    affected = []
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        low = s.lower()
        has_clear = any(w in low for w in ["away", "clear", "free", "cancel", "off"])
        has_range = ("weekend" in low) or (_find_day(s, _now_dt(S))[0] is not None)
        if has_clear and has_range:
            rng = _range_for(S, s)
            if rng is not None:
                start, end, _label = rng
                for it in _overlaps(S, start, end):
                    if it["id"] in affected:
                        continue
                    affected.append(it["id"])
                    ops.append({"op": "delete", "item_id": it["id"],
                                "before": _copy_item(it), "after": None})
            continue
        item = _text_create(S, s)
        if item is not None:
            ops.append({"op": "create", "item_id": None, "before": None, "after": item})

    plan = {
        "id": _new_id("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": affected,
    }
    S.setdefault("plans", {})[plan["id"]] = plan
    _log(S, "import", affected, {"filename": filename, "ops": len(ops)}, "text")
    return {"plan": plan}


# ---------------------------------------------------------------------------
# import_file
# ---------------------------------------------------------------------------

def import_file(S, filename, content):
    filename = filename or ""
    content = content or ""
    try:
        if "BEGIN:VCALENDAR" in content.upper() or filename.lower().endswith(".ics"):
            return _import_ics(S, filename, content)
        return _import_text(S, filename, content)
    except Exception:
        plan = {
            "id": _new_id("plan_"),
            "origin": "import",
            "status": "preview",
            "ops": [],
            "affected": [],
        }
        S.setdefault("plans", {})[plan["id"]] = plan
        return {"plan": plan}
