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

STOP_WORDS = set("""move reschedule push shift postpone bump delay put off back by
later earlier cancel delete remove drop scrap skip my the a an to on at for next
week weeks day days one please it""".split())

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

CLEAR_PHRASES = ["empty", "clear", "clear out", "free up", "wipe", "nothing on",
                 "no plans", "keep", "block out", "cancel everything", "cancel all",
                 "make room"]

QUERY_STARTS = ["what", "what's", "whats", "anything", "any", "show", "when",
                "do i have", "is there", "am i"]

CANCEL_STARTS = ["cancel", "delete", "remove", "drop", "scrap", "skip",
                 "get rid of", "forget", "i don't need", "i dont need"]

MOVE_STARTS = ["move", "reschedule", "push", "shift", "postpone", "bump",
               "delay", "put off", "pull"]

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
    "evening": (18, 0, 21, 0),
    "tonight": (20, 0, 22, 0),
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _uid(prefix):
    return prefix + uuid.uuid4().hex[:10]


def _now_dt(S):
    try:
        return datetime.strptime(S.get("now") or "2026-10-19T20:00", "%Y-%m-%dT%H:%M")
    except Exception:
        return datetime(2026, 10, 19, 20, 0)


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except Exception:
        return None


def fmt_iso(dt):
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M")


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


def _msg(S, kind, text, item_id=None, undo_id=None, steps=None):
    m = {
        "id": _uid("msg_"),
        "role": "agent",
        "kind": kind,
        "text": _clean(text),
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": undo_id,
    }
    if kind == "breakdown" and steps is not None:
        m["steps"] = steps
    S.setdefault("messages", []).append(m)
    return m


def _default_item():
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
        "source": "typed",
    }


def _full_item(base):
    it = _default_item()
    if isinstance(base, dict):
        for k, v in base.items():
            it[k] = v
    return it


def _items(S):
    return S.setdefault("items", [])


def _find_item(S, item_id):
    for it in _items(S):
        if it.get("id") == item_id:
            return it
    return None


def _plans(S):
    return S.setdefault("plans", {})


def _log(S, kind, item_ids, payload, source):
    try:
        v2.add_log(S, kind, item_ids, payload, source)
    except Exception:
        pass


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _match_words(text):
    return set(w for w in _words(text) if w not in STOP_WORDS)


def _best_match(S, text):
    mw = _match_words(text)
    best = None
    best_score = 0
    best_start = None
    for it in _items(S):
        if it.get("status") != "open":
            continue
        title_words = [w for w in _words(it.get("title") or "") if len(w) >= 3]
        score = 0
        for w in title_words:
            if w in mw:
                score += 1
        if score <= 0:
            continue
        st = parse_iso(it.get("start"))
        if best is None or score > best_score:
            best = it
            best_score = score
            best_start = st
        elif score == best_score:
            if st is not None and (best_start is None or st < best_start):
                best = it
                best_start = st
    return best


# ---------------------------------------------------------------------------
# day words
# ---------------------------------------------------------------------------

def _soonest_weekday(today_d, wd):
    delta = (wd - today_d.weekday()) % 7
    if delta == 0:
        delta = 7
    return today_d + timedelta(days=delta)


def _find_day(text, now_dt):
    """Return (day_d, matched_phrase) or (None, None)."""
    low = (text or "").lower()
    today_d = now_dt.date()

    # ISO date
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, m.group(0)
        except Exception:
            pass

    # Month name + day
    month_names = "|".join(sorted(MONTHS.keys(), key=len, reverse=True))
    m = re.search(r"\b(" + month_names + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
    if m:
        mon = MONTHS.get(m.group(1))
        dayn = int(m.group(2))
        if mon:
            year = today_d.year
            try:
                d = date(year, mon, dayn)
            except Exception:
                d = None
            if d is not None:
                if d < today_d:
                    try:
                        d = date(year + 1, mon, dayn)
                    except Exception:
                        pass
                return d, m.group(0)

    # today / tomorrow
    if re.search(r"\btoday\b", low):
        return today_d, "today"
    if re.search(r"\btomorrow\b", low):
        return today_d + timedelta(days=1), "tomorrow"

    # weekday names
    wd_names = "|".join(sorted(WEEKDAYS.keys(), key=len, reverse=True))
    m = re.search(r"\b(" + wd_names + r")\b", low)
    if m:
        wd = WEEKDAYS.get(m.group(1))
        if wd is not None:
            return _soonest_weekday(today_d, wd), m.group(0)

    # bare day of month
    m = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", low)
    if m:
        dayn = int(m.group(1))
        if 1 <= dayn <= 31:
            year = today_d.year
            mon = today_d.month
            try:
                d = date(year, mon, dayn)
            except Exception:
                d = None
            if d is None or d < today_d:
                mon2 = mon + 1
                year2 = year
                if mon2 > 12:
                    mon2 = 1
                    year2 += 1
                try:
                    d = date(year2, mon2, dayn)
                except Exception:
                    d = None
            if d is not None:
                return d, m.group(0)

    return None, None


def _find_part_of_day(text):
    low = (text or "").lower()
    for name in ("morning", "afternoon", "evening", "tonight"):
        if re.search(r"\b" + name + r"\b", low):
            return name
    return None


def _day_range(day_d, part):
    if part and part in PART_OF_DAY:
        h0, h1 = PART_OF_DAY[part]
        start = datetime.combine(day_d, dtime(h0, 0))
        if h1 >= 24:
            end = datetime.combine(day_d + timedelta(days=1), dtime(0, 0))
        else:
            end = datetime.combine(day_d, dtime(h1, 0))
        return start, end
    start = datetime.combine(day_d, dtime(0, 0))
    end = datetime.combine(day_d + timedelta(days=1), dtime(0, 0))
    return start, end


def _weekend_range(now_dt):
    today_d = now_dt.date()
    wd = today_d.weekday()
    if wd == 5:  # saturday
        sat = today_d
    elif wd == 6:  # sunday
        sat = today_d - timedelta(days=1)
    else:
        sat = today_d + timedelta(days=(5 - wd))
    start = datetime.combine(sat, dtime(0, 0))
    end = datetime.combine(sat + timedelta(days=2), dtime(0, 0))
    return start, end, sat


def _item_span(it):
    st = parse_iso(it.get("start"))
    if st is None:
        return None
    en = parse_iso(it.get("end"))
    if en is None:
        en = st + timedelta(minutes=60)
    return st, en


def _overlaps(it, rstart, rend):
    span = _item_span(it)
    if span is None:
        return False
    st, en = span
    return st < rend and en > rstart


# ---------------------------------------------------------------------------
# clock time parsing (for text import)
# ---------------------------------------------------------------------------

def _find_clock(text):
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
            return h, mi, m.group(0)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1))
        ap = m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return h, 0, m.group(0)
    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        if 0 <= h <= 23:
            return h, 0, m.group(0)
    return None


def _find_soft_window(text):
    low = (text or "").lower()
    for key in ("around noon", "noon", "lunch", "morning", "afternoon", "evening", "tonight"):
        if re.search(r"\b" + re.escape(key) + r"\b", low):
            return key
    return None


# ---------------------------------------------------------------------------
# BULK
# ---------------------------------------------------------------------------

def _has_clear_phrase(low):
    for p in CLEAR_PHRASES:
        if p in low:
            return True
    return False


def _purpose_title(low):
    purpose = None
    m = re.search(r"\bfor\s+(.+)$", low)
    if m:
        purpose = m.group(1)
    if not purpose:
        m = re.search(r",\s*(.+)$", low)
        if m:
            purpose = m.group(1)
    if not purpose:
        m = re.search(r"\b(?:because|since|as)\s+(.+)$", low)
        if m:
            purpose = m.group(1)
    if not purpose:
        return "Time off"
    purpose = purpose.strip().strip(".!?")
    # drop leading filler
    changed = True
    while changed:
        changed = False
        for lead in ["i have", "i've got", "i got", "i'm", "i am", "we have",
                     "we're", "there's", "a", "an", "my", "some", "please"]:
            if purpose.startswith(lead + " "):
                purpose = purpose[len(lead):].strip()
                changed = True
            elif purpose == lead:
                purpose = ""
                changed = True
    purpose = purpose.strip()
    if not purpose:
        return "Time off"
    return purpose[0].upper() + purpose[1:]


def _handle_bulk(S, low, now_dt):
    if not _has_clear_phrase(low):
        return None

    part = _find_part_of_day(low)
    range_words = None
    rstart = rend = None
    sat_d = None

    if re.search(r"\bweekend\b", low):
        rstart, rend, sat_d = _weekend_range(now_dt)
        range_words = "weekend"
        if part:
            h0, h1 = PART_OF_DAY[part]
            # clip part of day to the weekend range
            if part in ("morning", "afternoon"):
                rstart = datetime.combine(sat_d, dtime(h0, 0))
                rend = datetime.combine(sat_d, dtime(h1, 0))
            else:
                rstart = datetime.combine(sat_d, dtime(h0, 0))
                rend = datetime.combine(sat_d, dtime(23, 0))
            range_words = "weekend " + part
    else:
        day_d, phrase = _find_day(low, now_dt)
        if day_d is None:
            return None
        rstart, rend = _day_range(day_d, part)
        range_words = phrase
        if part:
            range_words = phrase + " " + part

    if rstart is None or rend is None:
        return None

    affected = []
    ops = []
    for it in _items(S):
        if it.get("status") != "open":
            continue
        if _overlaps(it, rstart, rend):
            affected.append(it.get("id"))
            ops.append({
                "op": "delete",
                "item_id": it.get("id"),
                "before": json.loads(json.dumps(it)),
                "after": None,
            })

    title = _purpose_title(low)

    # create op times
    if re.search(r"\bweekend\b", low):
        cstart = datetime.combine(sat_d, dtime(9, 0))
        cend = datetime.combine(sat_d + timedelta(days=1), dtime(18, 0))
        if part:
            h0, h1 = PART_OF_DAY[part]
            cstart = datetime.combine(sat_d, dtime(h0, 0))
            if h1 >= 24:
                cend = datetime.combine(sat_d, dtime(23, 0))
            else:
                cend = datetime.combine(sat_d, dtime(min(h1, 23), 0))
    else:
        day_d, _p = _find_day(low, now_dt)
        if part:
            h0, h1 = PART_OF_DAY[part]
            cstart = datetime.combine(day_d, dtime(h0, 0))
            cend = datetime.combine(day_d, dtime(min(h1, 23), 0))
        else:
            cstart = datetime.combine(day_d, dtime(9, 0))
            cend = datetime.combine(day_d, dtime(18, 0))

    new_item = _default_item()
    new_item["id"] = _uid("itm_")
    new_item["kind"] = "event"
    new_item["title"] = title
    new_item["start"] = fmt_iso(cstart)
    new_item["end"] = fmt_iso(cend)
    new_item["window"] = range_words
    new_item["status"] = "open"
    new_item["source"] = "typed"

    ops.append({
        "op": "create",
        "item_id": new_item["id"],
        "before": None,
        "after": new_item,
    })

    plan = {
        "id": _uid("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": ops,
        "affected": affected,
    }
    _plans(S)[plan["id"]] = plan

    n = len(affected)
    text = ("I can clear %d item%s and add '%s'. Shall I apply it?"
            % (n, "" if n == 1 else "s", title))
    m = _msg(S, "info", text)
    return {"messages": [m], "created": [], "plan": plan}


# ---------------------------------------------------------------------------
# QUERY
# ---------------------------------------------------------------------------

def _is_query(low):
    if low.endswith("?"):
        return True
    for s in QUERY_STARTS:
        if low.startswith(s + " ") or low == s:
            return True
    return False


def _handle_query(S, low, now_dt):
    if not _is_query(low):
        return None
    day_d, phrase = _find_day(low, now_dt)
    if day_d is None:
        day_d = now_dt.date()
        phrase = "today"
    part = _find_part_of_day(low)

    found = []
    for it in _items(S):
        st = parse_iso(it.get("start"))
        if st is None:
            continue
        if st.date() != day_d:
            continue
        if part:
            h0, h1 = PART_OF_DAY[part]
            if not (h0 <= st.hour < h1):
                continue
        found.append(it)

    found.sort(key=lambda x: (parse_iso(x.get("start")) or now_dt))

    day_label = day_d.strftime("%A")
    if part:
        day_label = day_label + " " + part

    if not found:
        text = "%s looks free." % day_label
    else:
        parts = []
        for it in found:
            st = parse_iso(it.get("start"))
            when = st.strftime("%H:%M") if st else ""
            if it.get("window"):
                when = it.get("window")
            parts.append("%s at %s" % (it.get("title"), when))
        text = "On %s: %s." % (day_label, ", ".join(parts))

    m = _msg(S, "reply", text)
    _log(S, "query", [it.get("id") for it in found],
         {"text": low, "day": day_d.isoformat()}, "typed")
    return {"messages": [m], "created": []}


# ---------------------------------------------------------------------------
# CANCEL
# ---------------------------------------------------------------------------

def _is_cancel(low):
    for s in CANCEL_STARTS:
        if low.startswith(s + " ") or low == s:
            return True
    return False


def _handle_cancel(S, low, now_dt):
    if not _is_cancel(low):
        return None
    it = _best_match(S, low)
    if it is None:
        m = _msg(S, "info", "I couldn't find that one. Which item do you mean?")
        return {"messages": [m], "created": []}

    plan = {
        "id": _uid("plan_"),
        "origin": "command",
        "status": "preview",
        "ops": [{
            "op": "delete",
            "item_id": it.get("id"),
            "before": json.loads(json.dumps(it)),
            "after": None,
        }],
        "affected": [it.get("id")],
    }
    _plans(S)[plan["id"]] = plan
    m = _msg(S, "info", "I can remove '%s' for you. Apply the change?" % it.get("title"))
    return {"messages": [m], "created": [], "plan": plan}


# ---------------------------------------------------------------------------
# MOVE
# ---------------------------------------------------------------------------

def _is_move(low):
    for s in MOVE_STARTS:
        if low.startswith(s + " ") or low == s:
            return True
    return False


def _shift_for(S, low, it, now_dt):
    """Return (new_start_dt, new_end_dt) or None."""
    st = parse_iso(it.get("start"))
    if st is None:
        return None
    en = parse_iso(it.get("end"))
    length = None
    if en is not None:
        length = en - st

    # N weeks
    m = re.search(r"\b(\d+)\s+weeks?\b", low)
    if m:
        n = int(m.group(1))
        ns = st + timedelta(days=7 * n)
        return ns, (ns + length if length else None)

    # a week / next week / back a week / by a week / one week
    if re.search(r"\b(?:next|a|one|back a|by a)\s+week\b", low) or re.search(r"\bweek later\b", low):
        ns = st + timedelta(days=7)
        return ns, (ns + length if length else None)

    # N days
    m = re.search(r"\b(\d+)\s+days?\b", low)
    if m:
        n = int(m.group(1))
        ns = st + timedelta(days=n)
        return ns, (ns + length if length else None)

    # tomorrow / a day / back a day
    if re.search(r"\btomorrow\b", low) or re.search(r"\b(?:a|one|back a|by a)\s+day\b", low):
        ns = st + timedelta(days=1)
        return ns, (ns + length if length else None)

    # weekday name
    wd_names = "|".join(sorted(WEEKDAYS.keys(), key=len, reverse=True))
    m = re.search(r"\b(" + wd_names + r")\b", low)
    if m:
        wd = WEEKDAYS.get(m.group(1))
        if wd is not None:
            today_d = now_dt.date()
            delta = (wd - today_d.weekday()) % 7
            if delta == 0:
                delta = 7
            target_d = today_d + timedelta(days=delta)
            ns = datetime.combine(target_d, dtime(st.hour, st.minute))
            return ns, (ns + length if length else None)

    # clock time
    ct = _find_clock(low)
    if ct is not None:
        h, mi, _p = ct
        ns = datetime.combine(st.date(), dtime(h, mi))
        return ns, (ns + length if length else None)

    return None


def _handle_move(S, low, now_dt):
    if not _is_move(low):
        return None
    it = _best_match(S, low)
    if it is None:
        m = _msg(S, "info", "I couldn't find that one. Which item do you mean?")
        return {"messages": [m], "created": []}

    res = _shift_for(S, low, it, now_dt)
    if res is None:
        m = _msg(S, "info", "I couldn't find that one. Which item do you mean?")
        return {"messages": [m], "created": []}

    ns, ne = res
    undo_id = _uid("u_")
    S.setdefault("undo", {})[undo_id] = {
        "item_id": it.get("id"),
        "before": json.loads(json.dumps(it)),
        "start": it.get("start"),
        "end": it.get("end"),
        "window": it.get("window"),
        "postpone_count": it.get("postpone_count", 0),
    }
    old_start = it.get("start")
    it["start"] = fmt_iso(ns)
    if ne is not None:
        it["end"] = fmt_iso(ne)

    _log(S, "update", [it.get("id")],
         {"old_start": old_start, "new_start": it.get("start")}, "typed")

    when = ns.strftime("%a %b %d at %H:%M")
    m = _msg(S, "info", "Moved '%s' to %s." % (it.get("title"), when),
             item_id=it.get("id"), undo_id=undo_id)
    return {"messages": [m], "created": []}


# ---------------------------------------------------------------------------
# handle_command
# ---------------------------------------------------------------------------

def handle_command(S, text):
    if not isinstance(text, str):
        return None
    low = text.strip().lower()
    if not low:
        return None
    now_dt = _now_dt(S)

    r = _handle_bulk(S, low, now_dt)
    if r is not None:
        return r
    r = _handle_query(S, low, now_dt)
    if r is not None:
        return r
    r = _handle_cancel(S, low, now_dt)
    if r is not None:
        return r
    r = _handle_move(S, low, now_dt)
    if r is not None:
        return r
    return None


# ---------------------------------------------------------------------------
# PLANS
# ---------------------------------------------------------------------------

def apply_plan(S, plan_id):
    plans = _plans(S)
    if plan_id not in plans:
        raise KeyError(plan_id)
    plan = plans[plan_id]
    if plan.get("status") != "preview":
        return {"plan": plan}

    created_ids = []
    for op in plan.get("ops", []):
        kind = op.get("op")
        if kind == "delete":
            iid = op.get("item_id")
            items = _items(S)
            for idx, it in enumerate(items):
                if it.get("id") == iid:
                    del items[idx]
                    break
            _log(S, "delete", [iid], {"plan": plan_id}, "agent")
        elif kind == "create":
            after = op.get("after")
            if after:
                new_it = json.loads(json.dumps(after))
                _items(S).append(new_it)
                created_ids.append(new_it.get("id"))
                _log(S, "create", [new_it.get("id")], {"plan": plan_id}, "agent")
        elif kind == "update":
            after = op.get("after")
            iid = op.get("item_id")
            if after:
                items = _items(S)
                for idx, it in enumerate(items):
                    if it.get("id") == iid:
                        items[idx] = json.loads(json.dumps(after))
                        break
                _log(S, "update", [iid], {"plan": plan_id}, "agent")

    plan["status"] = "applied"
    affected = list(plan.get("affected") or [])
    _log(S, "plan_applied", affected + created_ids, {"plan": plan_id}, "agent")
    return {"plan": plan}


def undo_plan(S, plan_id):
    plans = _plans(S)
    if plan_id not in plans:
        raise KeyError(plan_id)
    plan = plans[plan_id]
    if plan.get("status") != "applied":
        return {"plan": plan}

    items = _items(S)
    # remove created items
    for op in plan.get("ops", []):
        if op.get("op") == "create":
            iid = op.get("item_id")
            for idx, it in enumerate(items):
                if it.get("id") == iid:
                    del items[idx]
                    break

    # restore deleted / updated items
    for op in plan.get("ops", []):
        if op.get("op") in ("delete", "update"):
            before = op.get("before")
            iid = op.get("item_id")
            if not before:
                continue
            restored = json.loads(json.dumps(before))
            found = False
            for idx, it in enumerate(items):
                if it.get("id") == iid:
                    items[idx] = restored
                    found = True
                    break
            if not found:
                items.append(restored)

    plan["status"] = "undone"
    _log(S, "plan_undone", list(plan.get("affected") or []), {"plan": plan_id}, "agent")
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
                out[-1] = out[-1] + line[1:]
            else:
                out.append(line[1:])
        else:
            out.append(line)
    return out


def _parse_ics_date(value):
    """Return (iso_str, is_date_only) or (None, False)."""
    v = (value or "").strip()
    if not v:
        return None, False
    # strip trailing Z
    if v.endswith("Z"):
        v = v[:-1]
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", v)
    if m:
        return "%s-%s-%sT00:00" % (m.group(1), m.group(2), m.group(3)), True
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?$", v)
    if m:
        hh = m.group(4)
        mm = m.group(5)
        return "%s-%s-%sT%s:%s" % (m.group(1), m.group(2), m.group(3), hh, mm), False
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", v)
    if m:
        return "%s-%s-%sT%s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)), False
    return None, False


def _parse_ics(content):
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
        name = head.split(";")[0].strip().upper()
        cur[name] = value
    return events


def _import_ics(S, filename, content):
    events = _parse_ics(content)
    existing = _items(S)
    existing_uids = set()
    existing_title_start = set()
    for it in existing:
        if it.get("uid"):
            existing_uids.add(it.get("uid"))
        t = (it.get("title") or "").lower()
        s = it.get("start")
        if t and s:
            existing_title_start.add((t, s))

    ops = []
    affected = []
    for ev in events:
        uid = ev.get("UID")
        title = ev.get("SUMMARY") or "Untitled"
        start_iso, date_only = _parse_ics_date(ev.get("DTSTART"))
        end_iso, _ed = _parse_ics_date(ev.get("DTEND"))
        if start_iso is None:
            continue
        if end_iso is None:
            if date_only:
                sd = parse_iso(start_iso)
                if sd is not None:
                    end_iso = fmt_iso(sd + timedelta(days=1))
            else:
                sd = parse_iso(start_iso)
                if sd is not None:
                    end_iso = fmt_iso(sd + timedelta(minutes=60))

        if uid and uid in existing_uids:
            continue
        key = (title.lower(), start_iso)
        if key in existing_title_start:
            continue

        it = _default_item()
        it["id"] = _uid("itm_")
        it["kind"] = "event"
        it["title"] = title
        it["start"] = start_iso
        it["end"] = end_iso
        it["window"] = "all day" if date_only else None
        it["status"] = "open"
        it["source"] = "ics"
        it["uid"] = uid
        it["recurrence"] = ev.get("RRULE")

        existing_uids.add(uid)
        existing_title_start.add(key)

        ops.append({
            "op": "create",
            "item_id": it["id"],
            "before": None,
            "after": it,
        })
        affected.append(it["id"])

    plan = {
        "id": _uid("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": affected,
    }
    _plans(S)[plan["id"]] = plan
    _log(S, "import", affected,
         {"filename": filename, "ops": len(ops), "format": "ics"}, "ics")
    return {"plan": plan}


# ---------------------------------------------------------------------------
# TEXT import
# ---------------------------------------------------------------------------

def _split_sentences(content):
    parts = re.split(r"[.!?\n]+", content or "")
    return [p.strip() for p in parts if p.strip()]


def _import_text(S, filename, content):
    now_dt = _now_dt(S)
    ops = []
    affected = []
    seen_delete = set()

    for sent in _split_sentences(content):
        low = sent.lower()

        # delete sentence
        has_clear = any(w in low for w in ("away", "clear", "free", "cancel", "off"))
        has_range = bool(re.search(r"\bweekend\b", low)) or _find_day(low, now_dt)[0] is not None
        if has_clear and has_range:
            part = _find_part_of_day(low)
            if re.search(r"\bweekend\b", low):
                rstart, rend, sat_d = _weekend_range(now_dt)
                if part:
                    h0, h1 = PART_OF_DAY[part]
                    rstart = datetime.combine(sat_d, dtime(h0, 0))
                    rend = datetime.combine(sat_d, dtime(min(h1, 23), 0))
            else:
                day_d, _p = _find_day(low, now_dt)
                rstart, rend = _day_range(day_d, part)
            for it in _items(S):
                if it.get("status") != "open":
                    continue
                if it.get("id") in seen_delete:
                    continue
                if _overlaps(it, rstart, rend):
                    seen_delete.add(it.get("id"))
                    affected.append(it.get("id"))
                    ops.append({
                        "op": "delete",
                        "item_id": it.get("id"),
                        "before": json.loads(json.dumps(it)),
                        "after": None,
                    })
            continue

        # create sentence
        day_d, day_phrase = _find_day(low, now_dt)
        clock = _find_clock(low)
        soft = _find_soft_window(low)
        if day_d is None and clock is None and soft is None:
            continue

        if day_d is None:
            day_d = now_dt.date()

        start_dt = None
        end_dt = None
        window = None
        if clock is not None:
            h, mi, _p = clock
            start_dt = datetime.combine(day_d, dtime(h, mi))
            end_dt = start_dt + timedelta(minutes=60)
        elif soft is not None:
            h0, m0, h1, m1 = SOFT_WINDOWS.get(soft, (12, 0, 13, 0))
            start_dt = datetime.combine(day_d, dtime(h0, m0))
            end_dt = datetime.combine(day_d, dtime(h1, m1))
            window = soft
        else:
            start_dt = datetime.combine(day_d, dtime(9, 0))
            end_dt = datetime.combine(day_d, dtime(10, 0))

        # title
        title = sent
        title = re.sub(r"(?i)\b(also|on|at)\b", " ", title)
        if day_phrase:
            title = re.sub(re.escape(day_phrase), " ", title, flags=re.IGNORECASE)
        if clock is not None:
            title = re.sub(re.escape(clock[2]), " ", title, flags=re.IGNORECASE)
        if soft is not None:
            title = re.sub(re.escape(soft), " ", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+", " ", title).strip(" ,.;:-")
        if not title:
            title = "New item"
        title = title[0].upper() + title[1:]

        is_event = bool(re.search(r"\b(with|meet|lunch|dinner|call)\b", low))
        it = _default_item()
        it["id"] = _uid("itm_")
        it["kind"] = "event" if is_event else "task"
        it["title"] = title
        it["start"] = fmt_iso(start_dt)
        it["end"] = fmt_iso(end_dt)
        it["window"] = window
        it["status"] = "open"
        it["source"] = "text"

        ops.append({
            "op": "create",
            "item_id": it["id"],
            "before": None,
            "after": it,
        })
        affected.append(it["id"])

    plan = {
        "id": _uid("plan_"),
        "origin": "import",
        "status": "preview",
        "ops": ops,
        "affected": affected,
    }
    _plans(S)[plan["id"]] = plan
    _log(S, "import", affected,
         {"filename": filename, "ops": len(ops), "format": "text"}, "text")
    return {"plan": plan}


# ---------------------------------------------------------------------------
# import_file
# ---------------------------------------------------------------------------

def import_file(S, filename, content):
    filename = filename or ""
    content = content or ""
    is_ics = ("BEGIN:VCALENDAR" in content.upper()) or filename.lower().endswith(".ics")
    if is_ics:
        return _import_ics(S, filename, content)
    return _import_text(S, filename, content)
