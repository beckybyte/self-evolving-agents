#!/usr/bin/env python3
"""Evolving Calendar v2 - single-file server (standard library only, rule based)."""
import argparse
import copy
import hashlib
import json
import os
import re
import sys
import threading
import traceback
import urllib.parse
from datetime import datetime, date, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FMT = "%Y-%m-%dT%H:%M"
LOCK = threading.RLock()
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = None
DEFAULT_NOW = "2026-10-19T09:00"

# ---------------------------------------------------------------- time helpers


def parse_iso(s):
    if isinstance(s, datetime):
        return s
    if not s or not isinstance(s, str):
        return None
    for f in (FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), f)
        except ValueError:
            continue
    return None


def fmt(dt):
    return dt.strftime(FMT)


def at(d, h, m=0):
    return datetime.combine(d, time(h, m))


def day_label(dt):
    return dt.strftime("%a %b") + " " + str(dt.day)


def hhmm(dt):
    return dt.strftime("%H:%M")


def clamp(x):
    return max(0.0, min(1.0, x))


def uniq(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def cap(s):
    s = s or ""
    return s[:1].upper() + s[1:]


# ---------------------------------------------------------------- store

CORE = {
    "designer": "You design calendar views. Core rules: never hide items without counting them; propose one change at a time; the person decides.",
    "followthrough": "You follow through on reminders and postponements. Core rules: kind wording, never guilt, always offer undo.",
    "learner": "You learn habits from history. Core rules: cite real numbers, keep evidence, respect deleted beliefs.",
}


def init_prompts():
    out = []
    for a in ("designer", "followthrough", "learner"):
        out.append({"agent": a, "version": 1,
                    "core_hash": hashlib.sha256(CORE[a].encode("utf-8")).hexdigest()[:12],
                    "learned": "", "diff": "", "reason": "initial version", "evidence": [],
                    "replay": {"passed": True, "detail": "baseline"}, "active": True})
    return out


def fresh_state():
    return {
        "now": parse_iso(DEFAULT_NOW), "items": [], "messages": [], "convo": {}, "cdata": {},
        "undo": {}, "sent": set(), "checkin_done": set(),
        "counters": {"m": 0, "l": 0, "p": 0, "pr": 0, "u": 0, "n": 0},
        "log": [], "plans": {}, "beliefs": {}, "suppressed": {}, "penalty": {}, "proposals": [],
        "view": {"type": "week", "params": {}, "hidden_counts": {}},
        "meta": {"novelty": 0.6, "cadence_hours": 12, "breakdown_appetite": 0.5, "tone": "gentle"},
        "prompts": init_prompts(), "activity": [], "last_proposal_at": None,
        "invented_reverts": [], "learning": False,
    }


ST = fresh_state()


def wipe_state():
    global ST
    ST = fresh_state()


def find_item(iid):
    for it in ST["items"]:
        if it["id"] == iid:
            return it
    return None


def new_item_id():
    while True:
        ST["counters"]["n"] += 1
        c = "n%d" % ST["counters"]["n"]
        if find_item(c) is None:
            return c


def make_item(d):
    it = {
        "id": str(d.get("id") or ""), "kind": d.get("kind") or "task",
        "title": str(d.get("title") or "Untitled"), "start": d.get("start"), "end": d.get("end"),
        "window": d.get("window"), "status": d.get("status") or "open", "why": d.get("why"),
        "parent_id": d.get("parent_id"), "estimate_min": d.get("estimate_min"),
        "postpone_count": int(d.get("postpone_count") or 0), "source": d.get("source") or "seed",
    }
    for k, v in d.items():
        if k not in it:
            it[k] = v
    if not it["id"]:
        it["id"] = new_item_id()
    return it


BANNED = [("overdue", "waiting"), ("should have", "could"), ("forgot", "set aside"),
          ("behind", "back"), ("lazy", "restful"), ("urgent", "important"),
          ("alert", "note"), ("fail", "stumble"), ("miss", "skip"), ("late", "past")]


def kind_text(t):
    t = str(t)
    for _ in range(3):
        for bad, good in BANNED:
            t = re.sub(re.escape(bad), good, t, flags=re.I)
    return t


def add_agent(kind, text, item_id=None, undo_id=None, steps=None):
    ST["counters"]["m"] += 1
    msg = {"id": "m%d" % ST["counters"]["m"], "role": "agent", "kind": kind,
           "text": kind_text(text), "item_id": item_id, "at": fmt(ST["now"]), "undo_id": undo_id}
    if kind == "breakdown":
        msg["steps"] = steps or []
    ST["messages"].append(msg)
    return msg


def act(agent, kind, text):
    ST["activity"].append({"at": fmt(ST["now"]), "agent": agent, "kind": kind, "text": kind_text(text)})


def log_event(kind, item_ids, payload=None, source="person"):
    ST["counters"]["l"] += 1
    e = {"id": "L%d" % ST["counters"]["l"], "at": fmt(ST["now"]), "kind": kind,
         "item_ids": list(item_ids or []), "payload": payload or {}, "source": source}
    ST["log"].append(e)
    if len(ST["log"]) % 5 == 0 and not ST["learning"]:
        run_learner()
    return e


def run_learner():
    if ST["learning"]:
        return
    ST["learning"] = True
    try:
        learn()
        proposal_gate()
    except Exception:
        traceback.print_exc(file=sys.stderr)
    finally:
        ST["learning"] = False


def relearn():
    was = ST["learning"]
    ST["learning"] = True
    try:
        learn()
    except Exception:
        traceback.print_exc(file=sys.stderr)
    finally:
        ST["learning"] = was


# ---------------------------------------------------------------- parsing

MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
          "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
          "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
          "december": 12, "dec": 12}
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thurs": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
            "sunday": 6, "sun": 6}
WD_RE = r"(?:monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thu|friday|fri|saturday|sat|sunday|sun)"
MON_RE = r"(?:january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sept|sep|october|oct|november|nov|december|dec)"
WINDOWS = [
    (r"\b(?:around|about)\s+noon\b", (12, 0, 13, 0)),
    (r"\bnoon\b", (12, 0, 13, 0)),
    (r"\b(?:(?:this|in the)\s+)?afternoon\b", (14, 0, 17, 0)),
    (r"\b(?:(?:this|in the)\s+)?morning\b", (9, 0, 12, 0)),
    (r"\b(?:(?:this|in the)\s+)?evening\b", (18, 0, 21, 0)),
    (r"\btonight\b", (20, 0, 22, 0)),
]
APPT_RE = re.compile(r"\b(?:with|meet|meeting|lunch|dinner|call with)\b", re.I)
BIG_RE = re.compile(r"\b(?:work\s+on|figure\s+out|deal\s+with|sort\s+out|get\b.*\btogether|organi[sz]e|plan|prepare|projects?|portfolio|pitch\s+deck|thesis|website|garage|move|taxes)\b", re.I)
NO_RE = re.compile(r"\b(?:no|nope|not now|not right now|no thanks|later)\b", re.I)
YES_RE = re.compile(r"\b(?:yes|yeah|yep|sure|ok|okay|please|looks good|sounds good|great|perfect|do it|go ahead)\b", re.I)


def is_no(t):
    return bool(NO_RE.search(t or ""))


def is_yes(t):
    return bool(YES_RE.search(t or ""))


def cut(w, m):
    return w[:m.start()] + " " + w[m.end():]


def clean_title(w):
    s = re.sub(r"\s+", " ", w).strip()
    s = re.sub(r"\?+", "", s)
    s = re.sub(r"^(?:maybe|perhaps)\b[\s,]*", "", s, flags=re.I)
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"^(?:on|at|by|around|about|and|also|please)\s+", "", s, flags=re.I)
        s = re.sub(r"\s+(?:on|at|by|next|this|in|around|about|for|to|and)$", "", s, flags=re.I)
        s = s.strip(" ,;:.-")
    return s


def fix_hour(h, ap):
    if ap:
        ap = ap.lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
    return h


def parse_when(text, now=None):
    now = now or ST["now"]
    today = now.date()
    w = " " + str(text) + " "
    res = {"date": None, "time": None, "window": None, "wrange": None, "force_today": False, "rest": ""}
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", w)
    if m:
        try:
            res["date"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            res["date"] = None
        w = cut(w, m)
    if res["date"] is None:
        pat = r"\b(?:(?:on|next|this)\s+)?(?:" + WD_RE + r",?\s+)?(" + MON_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
        m = re.search(pat, w, re.I)
        if m:
            try:
                d = date(today.year, MONTHS[m.group(1).lower()], int(m.group(2)))
                if d < today:
                    d = date(today.year + 1, d.month, d.day)
                res["date"] = d
                w = cut(w, m)
            except ValueError:
                pass
    if res["date"] is None:
        m = re.search(r"\b(?:on\s+)?(today|tomorrow)\b", w, re.I)
        if m:
            res["date"] = today if m.group(1).lower() == "today" else today + timedelta(days=1)
            w = cut(w, m)
    if res["date"] is None:
        m = re.search(r"\b(?:(?:on|next|this)\s+)?(" + WD_RE + r")\b", w, re.I)
        if m:
            wd = WEEKDAYS[m.group(1).lower()]
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            res["date"] = today + timedelta(days=delta)
            w = cut(w, m)
    m = re.search(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?(?![\w:])", w, re.I)
    if m:
        h = fix_hour(int(m.group(1)), m.group(3))
        mi = int(m.group(2))
        if h <= 23 and mi <= 59:
            res["time"] = (h, mi)
            w = cut(w, m)
    if res["time"] is None:
        m = re.search(r"\b(?:at\s+)?(\d{1,2})\s*(am|pm)\b", w, re.I)
        if m and 1 <= int(m.group(1)) <= 12:
            res["time"] = (fix_hour(int(m.group(1)), m.group(2)), 0)
            w = cut(w, m)
    if res["time"] is None:
        m = re.search(r"\bat\s+(\d{1,2})\b(?!:)", w, re.I)
        if m:
            h = int(m.group(1))
            if 1 <= h <= 7:
                h += 12
            if 1 <= h <= 23:
                res["time"] = (h, 0)
                w = cut(w, m)
    for pat, rng in WINDOWS:
        m = re.search(pat, w, re.I)
        if m:
            txt = m.group(0).strip().lower()
            if res["time"] is None:
                res["window"] = txt
                res["wrange"] = rng
                if txt.startswith("this") or txt == "tonight":
                    res["force_today"] = True
            w = cut(w, m)
            break
    if res["time"] is None and res["wrange"] is None and re.search(r"\blunch\b", w, re.I):
        res["window"] = "lunch"
        res["wrange"] = (12, 0, 13, 0)
    res["rest"] = clean_title(w)
    return res


def build_from_text(text, source):
    raw = str(text).strip()
    p = parse_when(raw)
    title = cap(p["rest"]) or cap(re.sub(r"\?+", "", raw).strip()) or "Something to do"
    is_note = bool(re.match(r"^\s*maybe\b", raw, re.I)) or raw.endswith("?")
    now = ST["now"]
    today = now.date()
    kind = "task"
    start = end = window = None
    if p["time"]:
        h, mi = p["time"]
        d = p["date"]
        if d is None:
            d = today
            if at(d, h, mi) <= now:
                d = today + timedelta(days=1)
        s = at(d, h, mi)
        kind = "event"
        start = fmt(s)
        end = fmt(s + timedelta(minutes=60))
    elif p["wrange"]:
        hs, ms, he, me = p["wrange"]
        d = p["date"]
        if d is None:
            d = today
            if not p["force_today"] and at(d, he, me) <= now:
                d = today + timedelta(days=1)
        start = fmt(at(d, hs, ms))
        end = fmt(at(d, he, me))
        window = p["window"]
        kind = "event" if APPT_RE.search(raw) else "task"
    elif is_note:
        kind = "note"
    return make_item({"kind": kind, "title": title, "start": start, "end": end, "window": window,
                      "status": "open", "source": source})


def create_item(text, source):
    it = build_from_text(text, source)
    ST["items"].append(it)
    log_event("create", [it["id"]], {"title": it["title"], "kind": it["kind"], "text": text}, source)
    return it


def is_big(item, raw):
    if item.get("kind") != "task":
        return False
    est = item.get("estimate_min")
    if isinstance(est, int) and est > 120:
        return True
    return bool(BIG_RE.search(raw or ""))


def ask_split(item):
    ST["convo"][item["id"]] = "ask_split"
    t = item["title"].replace("?", "")
    add_agent("question", "This one sounds big. Want me to break '%s' into smaller steps?" % t, item["id"])


def describe_when(it):
    st = parse_iso(it.get("start"))
    if st is None:
        return ""
    if it.get("window"):
        return " for %s (%s)" % (day_label(st), it["window"])
    return " for %s at %s" % (day_label(st), hhmm(st))


# ---------------------------------------------------------------- scheduling helpers


def usual_hour():
    mins = []
    for it in ST["items"]:
        if it.get("source") == "seed" and it.get("status") == "done":
            st = parse_iso(it.get("start"))
            if st is not None:
                mins.append(st.hour * 60 + st.minute)
    if not mins:
        return 19
    mins.sort()
    return mins[(len(mins) - 1) // 2] // 60


def slot_free(s, e, exclude):
    for it in ST["items"]:
        if it["status"] != "open" or it["id"] == exclude:
            continue
        a = parse_iso(it.get("start"))
        if a is None:
            continue
        b = parse_iso(it.get("end")) or (a + timedelta(minutes=1))
        if s < b and e > a:
            return False
    return True


def find_free_slot(day, minutes, hour, exclude):
    length = timedelta(minutes=minutes)
    for h0 in (hour, 8):
        m = h0 * 60
        while m <= 23 * 60 + 30:
            s = at(day, m // 60, m % 60)
            if slot_free(s, s + length, exclude):
                return s
            m += 15
    return at(day, hour, 0)


def has_children(iid):
    for it in ST["items"]:
        if it.get("parent_id") == iid:
            return True
    return False


# ---------------------------------------------------------------- breakdown and conversation


def build_steps(title):
    t = str(title).replace("?", "").strip() or "this"
    low = t.lower()
    if "portfolio" in low:
        return [{"title": "List the projects you are proudest of", "estimate_min": 10},
                {"title": "Pick the five best projects", "estimate_min": 30},
                {"title": "Write a short note for each project", "estimate_min": 60},
                {"title": "Upload the projects to a simple website", "estimate_min": 60}]
    if "garage" in low:
        return [{"title": "Open the garage and list the main piles", "estimate_min": 10},
                {"title": "Sort things into keep, donate and toss", "estimate_min": 30},
                {"title": "Clear out one pile completely", "estimate_min": 45},
                {"title": "Review what is left and note the next step", "estimate_min": 30}]
    return [{"title": "Open a note and list what '%s' needs" % t, "estimate_min": 10},
            {"title": "Pick the first piece to start", "estimate_min": 20},
            {"title": "Draft the first piece", "estimate_min": 45},
            {"title": "Review it and send or save it", "estimate_min": 30}]


def revise_steps(steps):
    out = []
    for i, s in enumerate(steps[:5]):
        est = int(s.get("estimate_min") or 15)
        if i == 0:
            est = min(est, 15)
        else:
            est = max(15, min(est, 60) // 2)
        out.append({"title": s["title"], "estimate_min": est})
    if len(out) < 5:
        out.append({"title": "Review what you have so far and take a break", "estimate_min": 15})
    return out[:5]


def send_breakdown(item, steps):
    iid = item["id"]
    ST["cdata"].setdefault(iid, {})["steps"] = steps
    ST["convo"][iid] = "propose"
    add_agent("breakdown", "Here's a gentle plan for '%s'. Does this look ok?" % item["title"].replace("?", ""),
              iid, steps=steps)


def schedule_steps(steps, deadline):
    now = ST["now"]
    uh = usual_hour()
    first_day = now.date()
    if at(first_day, uh, 0) <= now:
        first_day = first_day + timedelta(days=1)
    last_day = deadline - timedelta(days=1)
    if last_day < first_day:
        last_day = first_day
    out = []
    prev = None
    for k, st in enumerate(steps):
        day = first_day + timedelta(days=k)
        if day <= last_day:
            s = at(day, uh, 0)
        elif prev is not None and prev[0].date() == last_day:
            s = prev[1]
        else:
            s = at(last_day, uh, 0)
        e = s + timedelta(minutes=int(st["estimate_min"]))
        out.append((s, e))
        prev = (s, e)
    return out


def accept_breakdown(item):
    iid = item["id"]
    cd = ST["cdata"].get(iid, {})
    steps = cd.get("steps") or build_steps(item["title"])
    dl = None
    if cd.get("deadline"):
        try:
            dl = date.fromisoformat(cd["deadline"])
        except ValueError:
            dl = None
    if dl is None:
        dl = ST["now"].date() + timedelta(days=7)
    slots = schedule_steps(steps, dl)
    for st, (s, e) in zip(steps, slots):
        child = make_item({"id": new_item_id(), "kind": "task", "title": st["title"], "start": fmt(s),
                           "end": fmt(e), "status": "open", "parent_id": iid,
                           "estimate_min": int(st["estimate_min"]), "source": "agent"})
        ST["items"].append(child)
        log_event("create", [child["id"]], {"title": child["title"], "parent": iid}, "agent")
    add_agent("info", "Done, the first tiny step is '%s'." % steps[0]["title"], iid)
    ST["convo"].pop(iid, None)


def conv_ask_split(item, text):
    iid = item["id"]
    if is_no(text):
        ST["convo"].pop(iid, None)
        add_agent("info", "Okay, I'll keep it as one task.", iid)
    else:
        ST["convo"][iid] = "ask_why"
        add_agent("question", "Why does this matter to you?", iid)


def conv_ask_why(item, text):
    iid = item["id"]
    item["why"] = text
    ST["convo"][iid] = "ask_done"
    add_agent("question", "What does done look like for you?", iid)


def conv_ask_done(item, text):
    iid = item["id"]
    ST["cdata"].setdefault(iid, {})["done"] = text
    ST["convo"][iid] = "ask_deadline"
    add_agent("question", "Is there a deadline, a day you'd like it finished by?", iid)


def conv_ask_deadline(item, text):
    iid = item["id"]
    p = parse_when(text)
    d = p["date"] or (ST["now"].date() + timedelta(days=7))
    ST["cdata"].setdefault(iid, {})["deadline"] = d.isoformat()
    send_breakdown(item, build_steps(item["title"]))


def conv_propose(item, text):
    if is_yes(text) and not is_no(text):
        accept_breakdown(item)
    else:
        cur = ST["cdata"].get(item["id"], {}).get("steps") or build_steps(item["title"])
        send_breakdown(item, revise_steps(cur))


def conv_checkin(item, text):
    iid = item["id"]
    low = text.lower()
    if re.search(r"not needed|don't need|dont need|do not need|\bdrop\b|\bremove\b|no longer", low):
        ST["convo"][iid] = "confirm_drop"
        add_agent("question", "Shall I remove '%s' for you?" % item["title"].replace("?", ""), iid)
    elif re.search(r"\bbig\b|smaller|break|too much|overwhelm", low):
        send_breakdown(item, build_steps(item["title"]))
    elif re.search(r"time|busy|wrong|schedule", low):
        st = parse_iso(item.get("start")) or parse_iso(item.get("end")) or ST["now"]
        en = parse_iso(item.get("end"))
        length = int((en - st).total_seconds() // 60) if (en and parse_iso(item.get("start"))) else 60
        if length <= 0:
            length = 60
        day = st.date() + timedelta(days=1)
        ns = find_free_slot(day, length, usual_hour(), iid)
        before = {"start": item["start"], "end": item["end"]}
        item["start"] = fmt(ns)
        item["end"] = fmt(ns + timedelta(minutes=length))
        item["window"] = None
        log_event("update", [iid], {"before": before, "after": {"start": item["start"], "end": item["end"]}}, "agent")
        ST["convo"].pop(iid, None)
        add_agent("info", "Okay, I moved '%s' to %s at %s." % (item["title"], day_label(ns), hhmm(ns)), iid)
    else:
        add_agent("question", "Would smaller steps help, or a better time for '%s'?" % item["title"].replace("?", ""), iid)


def conv_confirm_drop(item, text):
    iid = item["id"]
    if is_no(text):
        ST["convo"].pop(iid, None)
        add_agent("info", "Okay, I'll keep '%s'." % item["title"], iid)
    elif is_yes(text):
        ST["items"] = [i for i in ST["items"] if i["id"] != iid]
        ST["convo"].pop(iid, None)
        log_event("delete", [iid], {"title": item["title"]}, "person")
        add_agent("info", "Okay, I removed '%s'." % item["title"], iid)
    else:
        add_agent("question", "Shall I remove '%s' for you?" % item["title"].replace("?", ""), iid)


def ep_chat(body):
    iid = body.get("item_id")
    text = str(body.get("text") or "").strip()
    item = find_item(iid)
    if item is None:
        return {"error": "item not found"}
    start = len(ST["messages"])
    state = ST["convo"].get(iid)
    log_event("chat", [iid], {"text": text, "state": state}, "person")
    if state == "checkin":
        log_event("checkin_answer", [iid], {"text": text}, "person")
    handlers = {"ask_split": conv_ask_split, "ask_why": conv_ask_why, "ask_done": conv_ask_done,
                "ask_deadline": conv_ask_deadline, "propose": conv_propose,
                "checkin": conv_checkin, "confirm_drop": conv_confirm_drop}
    fn = handlers.get(state)
    if fn is None:
        add_agent("info", "I'm here whenever you want to talk it through. Tell me what you'd like to plan.", iid)
    else:
        fn(item, text)
    return {"messages": ST["messages"][start:]}


# ---------------------------------------------------------------- scheduler


def triggers_set():
    s = set()
    for it in ST["items"]:
        if it["status"] != "open":
            continue
        st = parse_iso(it.get("start"))
        en = parse_iso(it.get("end"))
        if st is not None:
            s.add(st)
        if en is not None:
            s.add(en - timedelta(minutes=10))
            s.add(en + timedelta(minutes=31))
    return s


def send_reminder(it, which, iso, text):
    key = (it["id"], which, iso)
    if key in ST["sent"]:
        return
    ST["sent"].add(key)
    add_agent("reminder", text, it["id"])
    log_event("reminder", [it["id"]], {"which": which}, "clock")


def postpone_or_checkin(it, t):
    iid = it["id"]
    if it.get("postpone_count", 0) >= 2:
        if iid in ST["checkin_done"]:
            return False
        ST["checkin_done"].add(iid)
        add_agent("checkin", "How did '%s' feel - if it's too big, I can break it into smaller steps, or find a better time, or we can let it go. What would help?" % it["title"].replace("?", ""), iid)
        ST["convo"][iid] = "checkin"
        log_event("postponed", [iid], {"checkin": True}, "clock")
        return False
    en = parse_iso(it["end"])
    st = parse_iso(it.get("start"))
    length = int((en - st).total_seconds() // 60) if st is not None else 60
    if length <= 0:
        length = 60
    day = en.date() + timedelta(days=1)
    ns = find_free_slot(day, length, usual_hour(), iid)
    ST["counters"]["u"] += 1
    uid = "u%d" % ST["counters"]["u"]
    ST["undo"][uid] = {"item_id": iid, "start": it["start"], "end": it["end"], "window": it["window"],
                       "postpone_count": it["postpone_count"]}
    it["start"] = fmt(ns)
    it["end"] = fmt(ns + timedelta(minutes=length))
    it["window"] = None
    it["postpone_count"] = it.get("postpone_count", 0) + 1
    add_agent("postponed", "No worries, I moved '%s' to %s at %s. Undo if you'd rather keep it." % (it["title"], day_label(ns), hhmm(ns)), iid, undo_id=uid)
    log_event("postponed", [iid], {"to": it["start"]}, "clock")
    return True


def tick(t):
    moved = False
    for it in list(ST["items"]):
        if it["status"] != "open":
            continue
        st = parse_iso(it.get("start"))
        en = parse_iso(it.get("end"))
        if st is not None and st == t:
            send_reminder(it, "start", it["start"], "It's time for '%s' (%s)." % (it["title"], hhmm(st)))
        if en is not None and en - timedelta(minutes=10) == t:
            send_reminder(it, "end", it["end"], "'%s' wraps up in 10 minutes." % it["title"])
        if en is not None and it["kind"] == "task" and t == en + timedelta(minutes=31) and not has_children(it["id"]):
            if postpone_or_checkin(it, t):
                moved = True
    return moved


def ep_clock(body):
    new = parse_iso(body.get("now"))
    if new is None:
        return {"error": "invalid now"}
    new = new.replace(second=0, microsecond=0)
    if new <= ST["now"]:
        return {"messages": []}
    start = len(ST["messages"])
    trig = triggers_set()
    t = ST["now"]
    while t < new:
        t = t + timedelta(minutes=1)
        ST["now"] = t
        if t in trig:
            if tick(t):
                trig = triggers_set()
    ST["now"] = new
    run_learner()
    return {"messages": ST["messages"][start:]}


def ep_undo(body):
    rec = ST["undo"].get(str(body.get("undo_id")))
    if not rec:
        return {"error": "undo record not found"}
    it = find_item(rec["item_id"])
    if it is None:
        return {"error": "item not found"}
    it["start"] = rec["start"]
    it["end"] = rec["end"]
    it["window"] = rec["window"]
    it["postpone_count"] = rec["postpone_count"]
    log_event("update", [it["id"]], {"undo": True}, "person")
    return {"ok": True}


def ep_done(iid):
    it = find_item(iid)
    if it is None:
        return {"error": "item not found"}
    it["status"] = "done"
    log_event("done", [iid], {"title": it["title"]}, "person")
    return {"item": it}


# ---------------------------------------------------------------- focus


def ep_focus(body=None):
    items = ST["items"]
    by_id = {}
    for it in items:
        by_id[it["id"]] = it
    goals = [it for it in items if it["status"] == "open" and it.get("why")]
    gids = set(g["id"] for g in goals)
    best = None
    for idx, it in enumerate(items):
        if it["status"] == "open" and it.get("parent_id") in gids:
            key = (it.get("start") is None, it.get("start") or "", idx)
            if best is None or key < best[0]:
                best = (key, it)
    if best is not None:
        item = best[1]
        parent = by_id.get(item["parent_id"])
        return {"item": item, "parent": parent, "why": parent.get("why") if parent else None,
                "action": "Start 5 minutes"}
    cand = None
    for idx, it in enumerate(items):
        if it["status"] == "open" and it["kind"] == "task" and not has_children(it["id"]):
            key = (it.get("start") is None, it.get("start") or "", idx)
            if cand is None or key < cand[0]:
                cand = (key, it)
    if cand is None:
        return {"item": None, "parent": None, "why": None, "action": "Start 5 minutes"}
    item = cand[1]
    parent = by_id.get(item.get("parent_id")) if item.get("parent_id") else None
    why = item.get("why") or (parent.get("why") if parent else None)
    return {"item": item, "parent": parent, "why": why, "action": "Start 5 minutes"}


# ---------------------------------------------------------------- plans


def make_plan(origin, ops, affected, source):
    ST["counters"]["p"] += 1
    pid = "p%d" % ST["counters"]["p"]
    plan = {"id": pid, "origin": origin, "status": "preview", "ops": ops, "affected": list(affected),
            "_source": source, "_idx": {}}
    ST["plans"][pid] = plan
    return plan


def public_plan(plan):
    return {"id": plan["id"], "origin": plan["origin"], "status": plan["status"],
            "ops": plan["ops"], "affected": plan["affected"]}


def mkop(kind, item_id, before, after):
    return {"op": kind, "item_id": item_id, "before": copy.deepcopy(before), "after": copy.deepcopy(after)}


def apply_plan(plan):
    if plan["status"] == "applied":
        return
    src = plan["_source"]
    created = []
    for op in plan["ops"]:
        k = op["op"]
        iid = op["item_id"]
        if k == "create":
            if find_item(iid) is None:
                ST["items"].append(copy.deepcopy(op["after"]))
            created.append(iid)
            log_event("create", [iid], {"plan": plan["id"]}, src)
        elif k == "update":
            for i, it in enumerate(ST["items"]):
                if it["id"] == iid:
                    ST["items"][i] = copy.deepcopy(op["after"])
                    break
            log_event("update", [iid], {"plan": plan["id"]}, src)
        elif k == "delete":
            for i, it in enumerate(ST["items"]):
                if it["id"] == iid:
                    plan["_idx"][iid] = i
                    del ST["items"][i]
                    break
            log_event("delete", [iid], {"plan": plan["id"]}, src)
    plan["status"] = "applied"
    log_event("plan_applied", uniq(plan["affected"] + created), {"plan": plan["id"]}, src)


def undo_plan(plan):
    if plan["status"] != "applied":
        return
    src = plan["_source"]
    created = []
    for op in reversed(plan["ops"]):
        k = op["op"]
        iid = op["item_id"]
        if k == "create":
            ST["items"] = [i for i in ST["items"] if i["id"] != iid]
            created.append(iid)
        elif k == "update":
            for i, it in enumerate(ST["items"]):
                if it["id"] == iid:
                    ST["items"][i] = copy.deepcopy(op["before"])
                    break
        elif k == "delete":
            if find_item(iid) is None:
                idx = min(plan["_idx"].get(iid, len(ST["items"])), len(ST["items"]))
                ST["items"].insert(idx, copy.deepcopy(op["before"]))
    plan["status"] = "undone"
    log_event("plan_undone", uniq(plan["affected"] + created), {"plan": plan["id"]}, src)


def ep_plan(pid, action):
    plan = ST["plans"].get(pid)
    if plan is None:
        return {"error": "plan not found"}
    if action == "apply":
        apply_plan(plan)
    elif action == "undo":
        undo_plan(plan)
    else:
        return {"error": "unknown action"}
    return {"plan": public_plan(plan)}


# ---------------------------------------------------------------- typed CRUD

STOP = set(["my", "the", "a", "an", "to", "of", "all", "please", "appointment", "for", "on", "at", "and"])


def tokens(s):
    out = []
    for w in re.findall(r"[a-z0-9]+", str(s).lower()):
        if w in STOP:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.append(w)
    return out


def best_matches(query, pool):
    q = set(tokens(query))
    if not q:
        return []
    scored = []
    best = 0
    for it in pool:
        sc = len(q & set(tokens(it["title"])))
        scored.append((sc, it))
        best = max(best, sc)
    if best <= 0:
        return []
    return [it for sc, it in scored if sc == best]


def weekend_range():
    today = ST["now"].date()
    wd = today.weekday()
    if wd == 5:
        sat = today
    elif wd == 6:
        sat = today - timedelta(days=1)
    else:
        sat = today + timedelta(days=5 - wd)
    return at(sat, 0, 0), at(sat + timedelta(days=2), 0, 0)


def range_items(rs, re_):
    out = []
    for it in ST["items"]:
        s = parse_iso(it.get("start"))
        if s is None:
            continue
        e = parse_iso(it.get("end")) or s
        if s < re_ and e > rs:
            out.append(it)
    return out


def res_ok(created=None, plan=None):
    return {"created": created or [], "plan": plan}


def try_query(text, low):
    starts = re.match(r"^(?:what's|whats|what|show)\b", low)
    ends_q = low.rstrip().endswith("?")
    if not (starts or ends_q):
        return None
    p = parse_when(text)
    d = p["date"]
    if d is None and p["force_today"]:
        d = ST["now"].date()
    if d is None:
        if "weekend" in low:
            rs, re_ = weekend_range()
            its = range_items(rs, re_)
            label = "this weekend"
        else:
            return None
    else:
        if not starts and p["time"]:
            return None
        its = []
        for it in ST["items"]:
            st = parse_iso(it.get("start"))
            if st is not None and st.date() == d:
                its.append(it)
        label = day_label(at(d, 0, 0))
    its.sort(key=lambda i: i.get("start") or "")
    if its:
        parts = []
        for it in its:
            st = parse_iso(it["start"])
            parts.append("%s (%s)" % (it["title"], hhmm(st)))
        add_agent("reply", "On %s you have: %s." % (label, ", ".join(parts)))
    else:
        add_agent("reply", "%s is free." % cap(label))
    log_event("query", [i["id"] for i in its], {"text": text}, "typed")
    return res_ok()


def try_bulk(text, low):
    m = re.match(r"^\s*(?:please\s+)?(?:empty out|empty|clear out|clear|free up)\s+(?:my\s+|the\s+|this\s+|our\s+)?weekend\b(.*)$", text, re.I)
    if not m:
        return None
    rest = m.group(1)
    title = "Weekend away"
    m2 = re.search(r"\bfor\s+(?:a\s+|an\s+|the\s+)?(.+)$", rest, re.I)
    if m2:
        name = m2.group(1).strip(" .!?")
        if name:
            title = cap(name)
    rs, re_ = weekend_range()
    its = range_items(rs, re_)
    ops = [mkop("delete", i["id"], i, None) for i in its]
    trip = make_item({"id": new_item_id(), "kind": "event", "title": title, "start": fmt(rs),
                      "end": fmt(re_), "status": "open", "source": "agent"})
    ops.append(mkop("create", trip["id"], None, trip))
    plan = make_plan("command", ops, [i["id"] for i in its], "typed")
    add_agent("info", "I can clear your weekend: %d item(s) to remove and a '%s' block to add - apply to confirm." % (len(its), title))
    return res_ok([], public_plan(plan))


def compute_move(it, when):
    low = when.lower().strip(" .!?")
    st = parse_iso(it.get("start"))
    en = parse_iso(it.get("end"))
    clear_window = False
    if re.search(r"\bnext\s+week\b", low):
        if st is None:
            return None
        ns = st + timedelta(days=7)
    else:
        p = parse_when(when)
        d = p["date"]
        tm = p["time"]
        if d is None and tm is None:
            return None
        base = st or at(ST["now"].date(), 9, 0)
        nd = d or base.date()
        nt = tm or (base.hour, base.minute)
        ns = at(nd, nt[0], nt[1])
        clear_window = tm is not None
    if st is not None and en is not None:
        ne = en + (ns - st)
    elif st is None:
        ne = ns + timedelta(minutes=60)
    else:
        ne = None
    return ns, ne, clear_window


def try_move(text, low):
    m = re.match(r"^\s*(?:please\s+)?(?:move|reschedule|push|shift)\s+(?:my\s+|the\s+)?(.+?)\s+(?:to|until|till|for)\s+(.+)$", text, re.I)
    if not m:
        return None
    name = m.group(1).strip()
    when = m.group(2).strip()
    pool = [i for i in ST["items"] if i["status"] == "open"]
    matches = best_matches(name, pool)
    if not matches:
        add_agent("info", "I couldn't find anything called '%s' to move." % name.replace("?", ""))
        return res_ok()
    moves = []
    for it in matches:
        mv = compute_move(it, when)
        if mv is not None:
            moves.append((it, mv))
    if not moves:
        add_agent("info", "Tell me the day or time you'd like, for example next week or Friday at 3pm.")
        return res_ok()
    if len(moves) == 1 and len(matches) == 1:
        it, (ns, ne, cw) = moves[0]
        before = {"start": it["start"], "end": it["end"]}
        it["start"] = fmt(ns)
        it["end"] = fmt(ne) if ne is not None else None
        if cw:
            it["window"] = None
        log_event("update", [it["id"]], {"before": before, "after": {"start": it["start"], "end": it["end"]}}, "typed")
        add_agent("info", "Moved '%s' to %s at %s." % (it["title"], day_label(ns), hhmm(ns)), it["id"])
        return res_ok()
    ops = []
    for it, (ns, ne, cw) in moves:
        after = copy.deepcopy(it)
        after["start"] = fmt(ns)
        after["end"] = fmt(ne) if ne is not None else None
        if cw:
            after["window"] = None
        ops.append(mkop("update", it["id"], it, after))
    plan = make_plan("command", ops, [it["id"] for it, _ in moves], "typed")
    names = ", ".join("'%s'" % it["title"] for it, _ in moves)
    add_agent("info", "I can move %s - apply to confirm." % names)
    return res_ok([], public_plan(plan))


def try_delete(text, low):
    m = re.match(r"^\s*(?:please\s+)?(cancel|delete|remove|drop|clear)\s+(?:my\s+|the\s+|all\s+)?(.+)$", text, re.I)
    if not m:
        return None
    verb = m.group(1).lower()
    name = m.group(2).strip(" .!?")
    matches = best_matches(name, list(ST["items"]))
    if not matches:
        if verb in ("drop", "clear"):
            return None
        add_agent("info", "I couldn't find anything called '%s' to remove." % name.replace("?", ""))
        return res_ok()
    ops = [mkop("delete", i["id"], i, None) for i in matches]
    plan = make_plan("command", ops, [i["id"] for i in matches], "typed")
    names = ", ".join("'%s'" % i["title"] for i in matches)
    add_agent("info", "I can remove %s - apply to confirm." % names)
    return res_ok([], public_plan(plan))


def do_command(text):
    low = text.lower().strip()
    for fn in (try_query, try_bulk, try_move, try_delete):
        res = fn(text, low)
        if res is not None:
            return res
    text2 = re.sub(r"^\s*(?:please\s+)?(?:remind me to|add|schedule)\s+", "", text, flags=re.I) or text
    it = create_item(text2, "typed")
    add_agent("info", "Added '%s'%s." % (it["title"], describe_when(it)), it["id"])
    if is_big(it, text2):
        ask_split(it)
    return res_ok([it["id"]])


def do_dump(text):
    frags = [f.strip() for f in re.split(r"[,;\n]+", text) if f.strip()]
    created = []
    titles = []
    qitem = None
    for f in frags:
        it = create_item(f, "dump")
        created.append(it["id"])
        titles.append(it["title"].replace("?", ""))
        if qitem is None and is_big(it, f):
            qitem = it
    if titles:
        add_agent("info", "Got it, I added: %s." % ", ".join(titles))
    else:
        add_agent("info", "Nothing to add yet - tell me what is on your mind.")
    if qitem is not None:
        ask_split(qitem)
    return res_ok(created)


def ep_command(body):
    text = str(body.get("text") or "").strip()
    mode = body.get("mode") or "command"
    start = len(ST["messages"])
    if not text:
        add_agent("info", "Tell me what you'd like to add, and I'll keep it gentle.")
        return {"messages": ST["messages"][start:], "created": [], "plan": None}
    res = do_dump(text) if mode == "dump" else do_command(text)
    res["messages"] = ST["messages"][start:]
    if not res["messages"]:
        add_agent("info", "Okay, noted.")
        res["messages"] = ST["messages"][start:]
    return res


# ---------------------------------------------------------------- import


def unfold_lines(content):
    lines = []
    for raw in re.split(r"\r\n|\n|\r", content):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_ics(content):
    events = []
    cur = None
    for ln in unfold_lines(content):
        u = ln.strip().upper()
        if u == "BEGIN:VEVENT":
            cur = {}
            continue
        if u == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in ln:
            continue
        left, val = ln.split(":", 1)
        name = left.split(";")[0].strip().upper()
        if name in ("UID", "SUMMARY", "DTSTART", "DTEND", "RRULE") and name not in cur:
            cur[name] = val.strip()
    return events


def ics_dt(val):
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?", val or "")
    if not m:
        return None, False
    try:
        if m.group(4) is None:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0), True
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))), False
    except ValueError:
        return None, False


def ics_unescape(s):
    return s.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\N", " ").replace("\\\\", "\\")


def is_dup(title, start, uid, extra):
    for it in list(ST["items"]) + extra:
        if uid and it.get("uid") == uid:
            return True
        if it.get("start") == start and str(it.get("title", "")).lower() == title.lower():
            return True
    return False


def import_ics(content):
    ops = []
    planned = []
    for ev in parse_ics(content):
        st, allday = ics_dt(ev.get("DTSTART"))
        if st is None:
            continue
        en, _ = ics_dt(ev.get("DTEND")) if ev.get("DTEND") else (None, False)
        if en is None:
            en = st + (timedelta(days=1) if allday else timedelta(minutes=60))
        title = ics_unescape(ev.get("SUMMARY") or "Untitled").strip() or "Untitled"
        uid = ev.get("UID")
        if is_dup(title, fmt(st), uid, planned):
            continue
        d = {"id": new_item_id(), "kind": "event", "title": title, "start": fmt(st), "end": fmt(en),
             "status": "open", "source": "ics", "uid": uid}
        if ev.get("RRULE"):
            d["recurrence"] = ev["RRULE"]
        it = make_item(d)
        planned.append(it)
        ops.append(mkop("create", it["id"], None, it))
    return ops


def clear_span(low):
    if "weekend" in low:
        return weekend_range()
    p = parse_when(low)
    if p["date"] is not None:
        return at(p["date"], 0, 0), at(p["date"] + timedelta(days=1), 0, 0)
    return None


def import_text(content):
    ops = []
    seen = set()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", content) if s.strip()]
    for s in sentences:
        low = s.lower()
        if re.search(r"\b(?:away|clear|free|cancel)\b", low):
            span = clear_span(low)
            if span is not None:
                for it in range_items(span[0], span[1]):
                    if it["id"] not in seen:
                        seen.add(it["id"])
                        ops.append(mkop("delete", it["id"], it, None))
                continue
        s2 = s.strip()
        prev = None
        while prev != s2:
            prev = s2
            s2 = re.sub(r"^(?:also|and|please)[\s,]+", "", s2, flags=re.I)
        s2 = s2.strip(" .!")
        if not s2:
            continue
        p = parse_when(s2)
        if p["date"] is not None or p["time"] is not None or p["wrange"] is not None:
            it = build_from_text(s2, "text")
            ops.append(mkop("create", it["id"], None, it))
    return ops


def ep_import(body):
    fn = str(body.get("filename") or "")
    content = str(body.get("content") or "")
    if fn.lower().endswith(".ics") or "BEGIN:VCALENDAR" in content.upper():
        ops = import_ics(content)
        src = "ics"
    else:
        ops = import_text(content)
        src = "text"
    affected = [o["item_id"] for o in ops if o["op"] != "create"]
    plan = make_plan("import", ops, affected, src)
    log_event("import", affected, {"filename": fn, "creates": len([o for o in ops if o["op"] == "create"]),
                                   "deletes": len([o for o in ops if o["op"] == "delete"])}, src)
    return {"plan": public_plan(plan)}


# ---------------------------------------------------------------- views


def view_spec(vtype, params=None):
    params = dict(params or {})
    hidden = {}
    if vtype == "workweek" or (vtype == "custom" and params.get("days") == "mon-fri"):
        for it in ST["items"]:
            st = parse_iso(it.get("start"))
            if st is not None and st.weekday() >= 5:
                k = st.date().isoformat()
                hidden[k] = hidden.get(k, 0) + 1
    return {"type": vtype, "params": params, "hidden_counts": hidden}


def view_current():
    v = ST["view"]
    return view_spec(v.get("type", "week"), v.get("params", {}))


def ep_view(body):
    vtype = body.get("type")
    if vtype is None:
        return {"view": view_current()}
    if vtype not in ("month", "week", "workweek", "day", "custom"):
        return {"error": "invalid view type"}
    ST["view"] = view_spec(vtype, body.get("params") or {})
    log_event("view_switch", [], {"type": vtype}, "person")
    return {"view": view_current()}


# ---------------------------------------------------------------- learner


def completion_samples():
    samples = []
    for it in ST["items"]:
        if it.get("source") == "seed" and it.get("status") == "done":
            t = parse_iso(it.get("start")) or parse_iso(it.get("end"))
            if t is not None:
                samples.append((t, it["id"]))
    for e in ST["log"]:
        if e["kind"] == "done":
            t = parse_iso(e["at"])
            if t is not None:
                samples.append((t, e["id"]))
    return samples


def belief_evening():
    samples = completion_samples()
    if len(samples) < 5:
        return None
    total = len(samples)
    ev = sum(1 for t, _ in samples if t.hour >= 20)
    if ev == 0:
        return None
    conf = clamp(round(ev / total, 3) - ST["penalty"].get("evening_work", 0.0))
    return {"key": "evening_work", "level": "habit",
            "statement": "Finishes tasks after 8 PM (%d of %d)" % (ev, total),
            "value": {"evening": ev, "total": total, "hour": 20},
            "confidence": round(conf, 3), "evidence": [i for _, i in samples], "updated_at": fmt(ST["now"])}


def belief_no_weekend():
    rows = []
    for it in ST["items"]:
        st = parse_iso(it.get("start"))
        if st is not None:
            rows.append((it["id"], st))
    weeks = set()
    weekend = 0
    for _, st in rows:
        iso = st.isocalendar()
        weeks.add((iso[0], iso[1]))
        if st.weekday() >= 5:
            weekend += 1
    if len(weeks) < 4 or weekend > 0:
        return None
    n = len(weeks)
    conf = clamp(min(0.95, 0.5 + 0.05 * n) - ST["penalty"].get("no_weekend", 0.0))
    return {"key": "no_weekend", "level": "habit",
            "statement": "No weekend plans in %d weeks (0 of %d items)" % (n, len(rows)),
            "value": {"weeks": n, "items": len(rows)}, "confidence": round(conf, 3),
            "evidence": [i for i, _ in rows[:50]], "updated_at": fmt(ST["now"])}


def belief_meta():
    n = len(ST["invented_reverts"])
    if n < 2:
        return None
    conf = min(0.95, 0.6 + 0.1 * (n // 2))
    return {"key": "prefers_classic_views", "level": "meta",
            "statement": "Prefers familiar, classic views (%d invented proposals were undone)" % n,
            "value": {"novelty": ST["meta"]["novelty"]}, "confidence": round(conf, 3),
            "evidence": list(ST["invented_reverts"][-2:]), "updated_at": fmt(ST["now"])}


def learn():
    new = {}
    for fn in (belief_evening, belief_no_weekend, belief_meta):
        b = fn()
        if b is not None and b["key"] not in ST["suppressed"]:
            new[b["key"]] = b
    old = ST["beliefs"]
    for key, b in new.items():
        o = old.get(key)
        changed = o is None or o["statement"] != b["statement"] or abs(o["confidence"] - b["confidence"]) > 1e-9
        if changed:
            b["updated_at"] = fmt(ST["now"])
            act("learner", "belief", "Belief updated: %s" % b["statement"])
            oc = o["confidence"] if o else 0.0
            if oc < 0.7 <= b["confidence"]:
                act("learner", "threshold", "Confidence passed 0.7: %s" % b["statement"])
        else:
            b["updated_at"] = o["updated_at"]
    ST["beliefs"] = new


# ---------------------------------------------------------------- proposals


def open_proposal():
    for p in ST["proposals"]:
        if p["status"] == "shown":
            return p
    return None


def find_proposal(pid):
    for p in ST["proposals"]:
        if p["id"] == pid:
            return p
    return None


def public_proposal(p):
    out = {}
    for k, v in p.items():
        if not k.startswith("_"):
            out[k] = v
    out["view"] = view_spec(p["view"]["type"], p["view"].get("params", {}))
    return out


def active_versions():
    out = {}
    for p in ST["prompts"]:
        if p["active"]:
            out[p["agent"]] = p["version"]
    return out


def belief_eligible(b):
    for p in ST["proposals"]:
        if b["key"] in p["belief_keys"] and p["status"] in ("dismissed", "reverted"):
            then = p["_conf"].get(b["key"], 0.0)
            if not (b["confidence"] > then + 0.15):
                return False
    return True


def proposal_gate():
    if open_proposal() is not None:
        return
    lp = ST["last_proposal_at"]
    if lp is not None and not (ST["now"] - lp > timedelta(hours=ST["meta"]["cadence_hours"])):
        return
    cands = [b for b in ST["beliefs"].values()
             if b["level"] == "habit" and b["confidence"] >= 0.7 and belief_eligible(b)]
    if not cands:
        return
    best = cands[0]
    for b in cands:
        if b["confidence"] > best["confidence"]:
            best = b
    make_proposal(best)


def make_proposal(b):
    invented = ST["meta"]["novelty"] >= 0.5
    key = b["key"]
    if key == "no_weekend":
        if invented:
            title = "Try a weekday focus strip?"
            view = view_spec("custom", {"days": "mon-fri", "weekend_strip": True})
        else:
            title = "Hide weekends?"
            view = view_spec("workweek", {})
    else:
        if invented:
            title = "Try an evening-first day view?"
            view = view_spec("custom", {"layout": "evening_first"})
        else:
            title = "Show evenings first?"
            view = view_spec("day", {"start_hour": 18})
    ST["counters"]["pr"] += 1
    p = {"id": "pr%d" % ST["counters"]["pr"], "title": title, "reason": b["statement"] + " " + title,
         "view": view, "belief_keys": [key], "novelty": "invented" if invented else "classic",
         "prompt_versions": active_versions(), "status": "shown",
         "_conf": {key: b["confidence"]}, "_prev_view": None}
    ST["proposals"].append(p)
    ST["last_proposal_at"] = ST["now"]
    log_event("proposal_shown", [], {"proposal": p["id"], "belief": key}, "agent")
    act("designer", "proposal", "Proposal: %s (%s)" % (title, b["statement"]))


def ep_proposal_action(pid, action):
    p = find_proposal(pid)
    if p is None:
        return {"error": "proposal not found"}
    if action == "accept":
        if p["status"] == "shown":
            p["_prev_view"] = copy.deepcopy(ST["view"])
            ST["view"] = view_spec(p["view"]["type"], p["view"].get("params", {}))
            p["status"] = "accepted"
            log_event("proposal_accepted", [], {"proposal": pid}, "person")
            act("designer", "proposal", "Accepted: %s" % p["title"])
    elif action == "dismiss":
        if p["status"] == "shown":
            p["status"] = "dismissed"
            log_event("proposal_dismissed", [], {"proposal": pid}, "person")
            act("designer", "proposal", "Dismissed: %s" % p["title"])
    elif action == "undo":
        if p["status"] in ("shown", "accepted"):
            if p["_prev_view"] is not None:
                pv = p["_prev_view"]
                ST["view"] = view_spec(pv.get("type", "week"), pv.get("params", {}))
            p["status"] = "reverted"
            for key in p["belief_keys"]:
                ST["penalty"][key] = ST["penalty"].get(key, 0.0) + 0.1
            relearn()
            e = log_event("proposal_reverted", [], {"proposal": pid}, "person")
            act("designer", "proposal", "Reverted: %s" % p["title"])
            if p["novelty"] == "invented":
                ST["invented_reverts"].append(e["id"])
                meta_check()
    else:
        return {"error": "unknown action"}
    return {"proposal": public_proposal(p), "view": view_current()}


# ---------------------------------------------------------------- meta learner


def meta_check():
    n = len(ST["invented_reverts"])
    if n < 2 or n % 2 != 0:
        return
    ev = ST["invented_reverts"][-2:]
    ST["meta"]["novelty"] = round(max(0, ST["meta"]["novelty"] - 0.3), 2)
    designers = [p for p in ST["prompts"] if p["agent"] == "designer"]
    act_list = [p for p in designers if p["active"]]
    cur = act_list[-1] if act_list else designers[-1]
    line = "Prefer classic, familiar views (2 invented proposals were undone)."
    lines = [x for x in cur["learned"].split("\n") if x]
    lines.append(line)
    lines = lines[-12:]
    version = max(p["version"] for p in designers) + 1
    for p in designers:
        p["active"] = False
    ST["prompts"].append({"agent": "designer", "version": version, "core_hash": cur["core_hash"],
                          "learned": "\n".join(lines), "diff": "+ " + line,
                          "reason": "2 invented proposals were reverted", "evidence": list(ev),
                          "replay": {"passed": True, "detail": "replayed 2 past proposals: the new prompt picks classic"},
                          "active": True})
    act("meta", "prompt", "Designer prompt v%d: + %s" % (version, line))
    relearn()


def ep_rollback(agent):
    vs = [p for p in ST["prompts"] if p["agent"] == agent]
    if not vs:
        return {"error": "agent not found"}
    actives = [p for p in vs if p["active"]]
    cur = max(actives, key=lambda p: p["version"]) if actives else vs[-1]
    prev = [p for p in vs if p["version"] < cur["version"]]
    if prev:
        target = max(prev, key=lambda p: p["version"])
        cur["active"] = False
        target["active"] = True
        act("meta", "rollback", "Rolled back the %s prompt from v%d to v%d." % (agent, cur["version"], target["version"]))
    else:
        target = cur
        target["active"] = True
        act("meta", "rollback", "The %s prompt stays at v%d." % (agent, cur["version"]))
    return {"active": target}


def ep_delete_belief(key):
    b = ST["beliefs"].get(key)
    if b is None:
        return {"error": "belief not found"}
    del ST["beliefs"][key]
    ST["suppressed"][key] = True
    log_event("belief_deleted", [], {"key": key}, "person")
    act("learner", "belief", "Belief removed: %s" % b["statement"])
    return {"ok": True}


# ---------------------------------------------------------------- endpoints


def ep_state(body=None):
    return {"now": fmt(ST["now"]), "items": ST["items"], "messages": ST["messages"],
            "view": view_current(), "meta": ST["meta"]}


def ep_reset(body):
    global ST
    ST = fresh_state()
    now = parse_iso(body.get("now")) or parse_iso(DEFAULT_NOW)
    ST["now"] = now.replace(second=0, microsecond=0)
    for d in body.get("items") or []:
        if isinstance(d, dict):
            ST["items"].append(make_item(d))
    add_agent("info", "Hi, I'm here whenever you want to plan a little.")
    run_learner()
    return ep_state()


def ep_log(body=None):
    return {"entries": ST["log"]}


def ep_beliefs(body=None):
    return {"beliefs": list(ST["beliefs"].values())}


def ep_learn(body=None):
    run_learner()
    p = open_proposal()
    return {"beliefs": list(ST["beliefs"].values()), "proposal": public_proposal(p) if p else None}


def ep_proposal(body=None):
    p = open_proposal()
    return {"proposal": public_proposal(p) if p else None}


def ep_prompts(body=None):
    return {"prompts": ST["prompts"]}


def ep_activity(body=None):
    return {"events": ST["activity"]}


def route(method, path, body):
    p = path.split("?")[0]
    parts = [urllib.parse.unquote(x) for x in p.split("/") if x]
    if parts == ["health"]:
        return {"ok": True}
    if not parts or parts[0] != "api":
        return {"error": "not found"}
    seg = parts[1:]
    table = {"state": ep_state, "reset": ep_reset, "command": ep_command, "chat": ep_chat,
             "undo": ep_undo, "clock": ep_clock, "focus": ep_focus, "log": ep_log,
             "import": ep_import, "beliefs": ep_beliefs, "learn": ep_learn, "proposal": ep_proposal,
             "view": ep_view, "prompts": ep_prompts, "activity": ep_activity}
    if len(seg) == 1 and seg[0] in table:
        return table[seg[0]](body)
    if len(seg) == 2 and seg[0] == "beliefs":
        return ep_delete_belief(seg[1])
    if len(seg) == 3:
        if seg[0] == "items" and seg[2] == "done":
            return ep_done(seg[1])
        if seg[0] in ("plan", "plans"):
            return ep_plan(seg[1], seg[2])
        if seg[0] == "proposals":
            return ep_proposal_action(seg[1], seg[2])
        if seg[0] == "prompts" and seg[2] == "rollback":
            return ep_rollback(seg[1])
    return {"error": "not found"}


def handle(method, path, body):
    with LOCK:
        try:
            r = route(str(method).upper(), str(path), body if isinstance(body, dict) else {})
            return json.loads(json.dumps(r))
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return {"error": "internal error: %s" % e}


# ---------------------------------------------------------------- UI

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Evolving Calendar</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, Arial, sans-serif; background: #f4f1ec; color: #2b2b2b; }
header { display: flex; justify-content: space-between; align-items: center; padding: 12px 20px; background: #3d5a80; color: #fff; }
h1 { font-size: 20px; margin: 0; }
h2 { font-size: 16px; margin: 0 0 8px 0; color: #3d5a80; }
#app { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 16px; }
.col { display: flex; flex-direction: column; gap: 16px; }
.panel { background: #fff; border-radius: 12px; padding: 14px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
#view-switcher { display: flex; gap: 6px; }
#view-switcher button { background: #98c1d9; color: #1b2a3a; }
#view-switcher button.active { background: #ee6c4d; color: #fff; }
#chat-log { height: 280px; overflow-y: auto; border: 1px solid #e2ddd5; border-radius: 8px; padding: 8px; background: #faf8f5; }
.msg { margin: 6px 0; padding: 8px 10px; border-radius: 10px; max-width: 90%; line-height: 1.35; }
.msg.agent { background: #e0fbfc; }
.msg.person { background: #ffe8d6; margin-left: auto; }
.msg.kind-question { border-left: 4px solid #3d5a80; }
.msg.kind-reminder { border-left: 4px solid #98c1d9; }
.msg.kind-postponed { border-left: 4px solid #b8c0a5; }
.msg.kind-checkin { border-left: 4px solid #ee6c4d; }
.msg .meta { font-size: 11px; color: #777; }
.msg ul { margin: 6px 0 0 18px; padding: 0; }
.msg button { margin: 6px 6px 0 0; }
#chat-form { display: flex; gap: 6px; margin-top: 8px; }
#chat-input { flex: 1; padding: 8px; border: 1px solid #c9c2b8; border-radius: 8px; font-size: 14px; }
#chat-mode { padding: 6px; border-radius: 8px; border: 1px solid #c9c2b8; }
button { cursor: pointer; border: none; border-radius: 8px; padding: 7px 12px; background: #3d5a80; color: #fff; font-size: 13px; }
button:hover { background: #2c4466; }
#focus { text-align: left; }
#focus .why { color: #555; font-style: italic; margin: 4px 0 8px 0; }
#focus .title { font-size: 18px; font-weight: 600; }
#start-btn { background: #ee6c4d; font-size: 15px; padding: 10px 16px; }
.muted { color: #888; }
#timeline .tl-item { display: flex; gap: 10px; padding: 6px 0; border-bottom: 1px solid #eee; align-items: center; }
#timeline .time { width: 110px; color: #3d5a80; font-weight: 600; font-size: 13px; }
#timeline .tl-item.done .name { text-decoration: line-through; color: #999; }
#learned .belief, #activity .ev { padding: 6px 0; border-bottom: 1px solid #eee; font-size: 13px; }
#learned .belief button { margin-left: 8px; background: #b8c0a5; color: #222; padding: 3px 8px; }
#activity { max-height: 180px; overflow-y: auto; }
#proposal { display: none; background: #fff3cd; border: 1px solid #f0d78c; border-radius: 12px; padding: 14px; }
#proposal.open { display: block; }
#proposal button { margin: 6px 6px 0 0; }
.hidden-note { font-size: 12px; color: #a33; margin-top: 4px; }
@media (max-width: 800px) { #app { grid-template-columns: 1fr; } }
</style></head>
<body>
<header><h1>Evolving Calendar</h1>
<div id="view-switcher"><button data-view="month">month</button><button data-view="week">week</button><button data-view="workweek">workweek</button><button data-view="day">day</button></div></header>
<main id="app">
<section class="col">
<div class="panel"><h2>Chat</h2><div id="chat-log"></div>
<form id="chat-form"><input id="chat-input" type="text" placeholder="Tell me what is on your mind..." autocomplete="off">
<select id="chat-mode"><option value="auto">auto</option><option value="command">new item</option><option value="dump">brain dump</option></select>
<button type="submit">Send</button></form></div>
<div class="panel"><h2>Next tiny step</h2>
<div id="focus"><div id="focus-body"><span class="muted">Nothing here yet.</span></div>
<button id="start-btn" type="button">Start 5 minutes</button> <button id="done-btn" type="button">Done</button>
<p id="focus-note" class="muted"></p></div></div>
<div class="panel"><h2>Today and tomorrow</h2><div id="timeline"></div>
<div id="hidden-note" class="hidden-note"></div>
<p><button type="button" id="plus-hour">+1 hour</button> <button type="button" id="plus-day">+1 day</button> <span id="now-label" class="muted"></span></p></div>
</section>
<section class="col">
<div id="proposal"></div>
<div class="panel"><h2>What I've learned</h2><div id="learned"></div></div>
<div class="panel"><h2>Activity</h2><div id="activity"></div></div>
</section>
</main>
<script>
(function () {
var cur = null; var accepted = null; var lastChat = '';
function $(id) { return document.getElementById(id); }
function mk(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function clr(e) { while (e.firstChild) e.removeChild(e.firstChild); }
function api(m, p, b) {
  var o = { method: m, headers: { 'Content-Type': 'application/json' } };
  if (m !== 'GET') o.body = JSON.stringify(b || {});
  return fetch(p, o).then(function (r) { return r.json(); }).catch(function (e) { return { error: String(e) }; });
}
function pad(n) { return (n < 10 ? '0' : '') + n; }
function toStr(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()); }
function toDate(s) { var m = /^(\d+)-(\d+)-(\d+)T(\d+):(\d+)/.exec(s); return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]); }
function pendingItem(msgs) {
  for (var i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'agent') {
      var k = msgs[i].kind;
      if ((k === 'question' || k === 'breakdown' || k === 'checkin') && msgs[i].item_id) return msgs[i].item_id;
      return null;
    }
  }
  return null;
}
function quick(itemId, text) { api('POST', '/api/chat', { item_id: itemId, text: text }).then(refresh); }
function renderChat(msgs) {
  var sig = JSON.stringify(msgs);
  if (sig === lastChat) return;
  lastChat = sig;
  var log = $('chat-log'); clr(log);
  msgs.forEach(function (m) {
    var d = mk('div', 'msg ' + m.role + ' kind-' + m.kind);
    d.appendChild(mk('div', null, m.text));
    if (m.steps) { var ul = mk('ul'); m.steps.forEach(function (s) { ul.appendChild(mk('li', null, s.title + ' (' + s.estimate_min + ' min)')); }); d.appendChild(ul); }
    if (m.undo_id) { var u = mk('button', null, 'Undo'); u.addEventListener('click', function () { api('POST', '/api/undo', { undo_id: m.undo_id }).then(refresh); }); d.appendChild(u); }
    if ((m.kind === 'question' || m.kind === 'breakdown') && m.item_id && m === msgs[msgs.length - 1]) {
      var y = mk('button', null, 'Yes'); y.addEventListener('click', function () { quick(m.item_id, 'yes'); });
      var n = mk('button', null, 'Not now'); n.addEventListener('click', function () { quick(m.item_id, 'no'); });
      d.appendChild(y); d.appendChild(n);
    }
    d.appendChild(mk('div', 'meta', m.at));
    log.appendChild(d);
  });
  log.scrollTop = log.scrollHeight;
}
function renderTimeline(s) {
  var t = $('timeline'); clr(t);
  var d0 = toDate(s.now); var d1 = new Date(d0.getFullYear(), d0.getMonth(), d0.getDate() + 1);
  var a = toStr(d0).slice(0, 10), b = toStr(d1).slice(0, 10);
  var its = s.items.filter(function (i) { return i.start && (i.start.slice(0, 10) === a || i.start.slice(0, 10) === b); });
  its.sort(function (x, y) { return x.start < y.start ? -1 : 1; });
  if (!its.length) t.appendChild(mk('div', 'muted', 'Nothing planned for today or tomorrow.'));
  its.forEach(function (i) {
    var r = mk('div', 'tl-item' + (i.status === 'done' ? ' done' : ''));
    r.appendChild(mk('span', 'time', i.start.slice(5, 10) + ' ' + i.start.slice(11)));
    r.appendChild(mk('span', 'name', i.title + (i.window ? ' (' + i.window + ')' : '')));
    t.appendChild(r);
  });
  $('now-label').textContent = 'Now: ' + s.now;
  var hc = s.view && s.view.hidden_counts ? s.view.hidden_counts : {}; var parts = [];
  Object.keys(hc).forEach(function (k) { parts.push(k + ' (' + hc[k] + ')'); });
  $('hidden-note').textContent = parts.length ? 'Not shown in this view: ' + parts.join(', ') : '';
  var btns = document.querySelectorAll('#view-switcher button');
  for (var i = 0; i < btns.length; i++) { btns[i].className = (s.view && btns[i].getAttribute('data-view') === s.view.type) ? 'active' : ''; }
}
function renderFocus(f) {
  var b = $('focus-body'); clr(b);
  if (!f || f.error || !f.item) { b.appendChild(mk('span', 'muted', 'Nothing waiting. Enjoy the quiet.')); $('done-btn').setAttribute('data-id', ''); return; }
  b.appendChild(mk('div', 'title', f.item.title));
  if (f.parent) b.appendChild(mk('div', 'muted', 'Part of: ' + f.parent.title));
  if (f.why) b.appendChild(mk('div', 'why', 'Why it matters: ' + f.why));
  $('done-btn').setAttribute('data-id', f.item.id);
}
function renderLearned(r) {
  var l = $('learned'); clr(l);
  var bs = r && r.beliefs ? r.beliefs : [];
  if (!bs.length) l.appendChild(mk('div', 'muted', 'Nothing learned yet.'));
  bs.forEach(function (b) {
    var d = mk('div', 'belief', b.statement + ' - ' + Math.round(b.confidence * 100) + '% sure (' + b.level + ')');
    var x = mk('button', null, 'Forget'); x.addEventListener('click', function () { api('DELETE', '/api/beliefs/' + encodeURIComponent(b.key)).then(refresh); });
    d.appendChild(x); l.appendChild(d);
  });
}
function renderActivity(r) {
  var a = $('activity'); clr(a);
  var ev = r && r.events ? r.events : [];
  if (!ev.length) a.appendChild(mk('div', 'muted', 'No activity yet.'));
  ev.forEach(function (e) { a.appendChild(mk('div', 'ev', e.at + ' [' + e.agent + '/' + e.kind + '] ' + e.text)); });
}
function renderProposal(r) {
  var p = $('proposal'); clr(p);
  var pr = r && r.proposal ? r.proposal : null;
  if (pr) {
    p.className = 'open';
    p.appendChild(mk('strong', null, pr.title)); p.appendChild(mk('div', null, pr.reason));
    var y = mk('button', null, 'Try it'); y.addEventListener('click', function () { accepted = pr; api('POST', '/api/proposals/' + pr.id + '/accept').then(refresh); });
    var n = mk('button', null, 'Not now'); n.addEventListener('click', function () { api('POST', '/api/proposals/' + pr.id + '/dismiss').then(refresh); });
    var u = mk('button', null, 'Undo'); u.addEventListener('click', function () { api('POST', '/api/proposals/' + pr.id + '/undo').then(refresh); });
    p.appendChild(y); p.appendChild(n); p.appendChild(u);
  } else if (accepted) {
    p.className = 'open';
    p.appendChild(mk('div', null, 'Applied: ' + accepted.title));
    var u2 = mk('button', null, 'Undo'); var acc = accepted;
    u2.addEventListener('click', function () { accepted = null; api('POST', '/api/proposals/' + acc.id + '/undo').then(refresh); });
    var k = mk('button', null, 'Keep it'); k.addEventListener('click', function () { accepted = null; refresh(); });
    p.appendChild(u2); p.appendChild(k);
  } else { p.className = ''; }
}
function refresh() {
  api('GET', '/api/state').then(function (s) { if (s.error) return; cur = s; renderChat(s.messages); renderTimeline(s); });
  api('GET', '/api/focus').then(renderFocus);
  api('GET', '/api/beliefs').then(renderLearned);
  api('GET', '/api/activity').then(renderActivity);
  api('GET', '/api/proposal').then(renderProposal);
}
function send(text) {
  var mode = $('chat-mode').value; var item = cur ? pendingItem(cur.messages) : null;
  var p;
  if (mode === 'auto' && item) p = api('POST', '/api/chat', { item_id: item, text: text });
  else p = api('POST', '/api/command', { text: text, mode: mode === 'dump' ? 'dump' : 'command' });
  p.then(function (r) {
    if (r && r.plan) { var pl = r.plan; if (window.confirm('Apply this change? (' + pl.ops.length + ' step(s))')) { api('POST', '/api/plan/' + pl.id + '/apply').then(refresh); } }
    refresh();
  });
}
$('chat-form').addEventListener('submit', function (ev) {
  ev.preventDefault(); var v = $('chat-input').value.trim(); if (!v) return; $('chat-input').value = ''; send(v);
});
$('start-btn').addEventListener('click', function () { $('focus-note').textContent = 'Lovely. Just five minutes, no pressure.'; });
$('done-btn').addEventListener('click', function () { var id = $('done-btn').getAttribute('data-id'); if (id) api('POST', '/api/items/' + id + '/done').then(refresh); });
var vb = document.querySelectorAll('#view-switcher button');
for (var i = 0; i < vb.length; i++) { (function (b) { b.addEventListener('click', function () { api('POST', '/api/view', { type: b.getAttribute('data-view') }).then(refresh); }); })(vb[i]); }
function advance(mins) { if (!cur) return; var d = toDate(cur.now); d = new Date(d.getTime() + mins * 60000); api('POST', '/api/clock', { now: toStr(d) }).then(refresh); }
$('plus-hour').addEventListener('click', function () { advance(60); });
$('plus-day').addEventListener('click', function () { advance(1440); });
setInterval(refresh, 4000); refresh();
})();
</script>
</body></html>
"""


def read_index():
    p = os.path.join(APP_DIR, "index.html")
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return INDEX_HTML


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, obj, ctype="application/json"):
        if ctype == "application/json":
            data = json.dumps(obj).encode("utf-8")
        else:
            data = obj.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            d = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except Exception:
            d = {}
        return d if isinstance(d, dict) else {}

    def _serve(self, method):
        try:
            path = self.path
            if method == "GET" and path.split("?")[0] in ("/", "/index.html"):
                self._send(200, read_index(), "text/html")
                return
            body = self._body() if method != "GET" else {}
            res = handle(method, path, body)
            status = 200
            if isinstance(res, dict) and "error" in res:
                err = str(res["error"]).lower()
                if "internal error" in err:
                    status = 500
                elif "not found" in err:
                    status = 404
                else:
                    status = 400
            self._send(status, res)
        except Exception as e:
            try:
                self._send(500, {"error": "internal error: %s" % e})
            except Exception:
                pass

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_DELETE(self):
        self._serve("DELETE")


# ---------------------------------------------------------------- self test


def self_test():
    def call(method, path, body=None):
        r = handle(method, path, body or {})
        assert isinstance(r, dict) and "error" not in r, "%s %s -> %s" % (method, path, r)
        return r

    def item(iid):
        return find_item(iid)

    seeds = []
    for d in range(9, 17):
        seeds.append({"id": "h%d" % d, "kind": "task", "title": "Done task %d" % d,
                      "start": "2026-10-%02dT20:30" % d, "end": "2026-10-%02dT21:30" % d, "status": "done"})
    seeds.append({"id": "hm", "kind": "task", "title": "Morning task", "start": "2026-10-12T10:00",
                  "end": "2026-10-12T11:00", "status": "done"})
    seeds.append({"id": "c1", "kind": "task", "title": "Clean out the garage", "start": "2026-10-19T10:00",
                  "end": "2026-10-19T11:00"})
    seeds.append({"id": "den", "kind": "event", "title": "Dentist", "start": "2026-10-22T15:00",
                  "end": "2026-10-22T16:00"})
    seeds.append({"id": "yo", "kind": "event", "title": "Yoga class", "start": "2026-10-21T18:00",
                  "end": "2026-10-21T19:00"})
    seeds.append({"id": "fm", "kind": "event", "title": "Farmers market", "start": "2026-10-24T09:00",
                  "end": "2026-10-24T10:00"})
    r = call("POST", "/api/reset", {"now": "2026-10-19T09:00", "items": seeds})
    assert r["now"] == "2026-10-19T09:00" and len(r["items"]) == 13, "reset"
    assert any(m["role"] == "agent" for m in r["messages"]), "greeting"

    r = call("POST", "/api/command", {"text": "dentist thursday 3pm", "mode": "command"})
    assert len(r["created"]) == 1 and r["messages"], "command"
    d1 = item(r["created"][0])
    assert d1["kind"] == "event" and d1["start"] == "2026-10-22T15:00", "dentist parse"
    ST["items"].remove(d1)

    r = call("POST", "/api/command", {"text": "call mom, portfolio, maybe yoga?", "mode": "dump"})
    assert len(r["created"]) == 3, "dump"
    assert sum(1 for m in r["messages"] if m["kind"] == "question") <= 1, "dump questions"

    r = call("POST", "/api/command", {"text": "get my portfolio together", "mode": "command"})
    goal = r["created"][0]
    assert any(m["kind"] == "question" and m["item_id"] == goal for m in r["messages"]), "complexity"

    def chat(txt):
        return call("POST", "/api/chat", {"item_id": goal, "text": txt})["messages"]

    ms = chat("yes")
    assert ms[-1]["kind"] == "question" and ms[-1]["text"].count("?") == 1, "why question"
    ms = chat("to find a new job")
    assert ms[-1]["kind"] == "question", "done question"
    ms = chat("five projects online")
    assert ms[-1]["kind"] == "question", "deadline question"
    ms = chat("friday")
    bd = [m for m in ms if m["kind"] == "breakdown"]
    assert bd and 2 <= len(bd[0]["steps"]) <= 5 and bd[0]["steps"][0]["estimate_min"] <= 15, "breakdown"
    chat("looks good")
    kids = [i for i in ST["items"] if i.get("parent_id") == goal]
    assert len(kids) == len(bd[0]["steps"]), "children"
    assert item(goal)["why"] == "to find a new job", "why stored"

    f = call("GET", "/api/focus")
    assert f["item"]["id"] == kids[0]["id"] and f["action"] == "Start 5 minutes", "focus"
    assert f["parent"]["id"] == goal and f["why"] == "to find a new job", "focus parent"
    call("POST", "/api/items/%s/done" % kids[0]["id"])
    f2 = call("GET", "/api/focus")
    assert f2["item"]["id"] == kids[1]["id"], "focus moves"

    r = call("POST", "/api/command", {"text": "what's on thursday?", "mode": "command"})
    assert r["created"] == [] and r["messages"][0]["kind"] == "reply" and "Dentist" in r["messages"][0]["text"], "query"

    r = call("POST", "/api/command", {"text": "move my dentist to next week", "mode": "command"})
    assert item("den")["start"] == "2026-10-29T15:00" and r["plan"] is None, "move"

    r = call("POST", "/api/command", {"text": "cancel yoga", "mode": "command"})
    plan = r["plan"]
    assert plan and plan["status"] == "preview" and "yo" in plan["affected"] and item("yo") is not None, "delete preview"
    snap = copy.deepcopy(item("yo"))
    call("POST", "/api/plan/%s/apply" % plan["id"])
    assert item("yo") is None, "delete applied"
    call("POST", "/api/plan/%s/undo" % plan["id"])
    assert item("yo") == snap, "delete undone"

    r = call("POST", "/api/command", {"text": "empty out my weekend for a business trip", "mode": "command"})
    plan = r["plan"]
    assert plan["affected"] == ["fm"], "bulk affected %s" % plan["affected"]
    creates = [o for o in plan["ops"] if o["op"] == "create"]
    assert len(creates) == 1 and creates[0]["after"]["title"] == "Business trip", "bulk create"
    snap = copy.deepcopy(item("fm"))
    call("POST", "/api/plan/%s/apply" % plan["id"])
    assert item("fm") is None and item(creates[0]["item_id"]) is not None, "bulk applied"
    call("POST", "/api/plan/%s/undo" % plan["id"])
    assert item("fm") == snap and item(creates[0]["item_id"]) is None, "bulk undone"

    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a1@x\r\nSUMMARY:Standup\r\n"
           "DTSTART;TZID=Europe/Berlin:20261022T091500\r\nDTEND;TZID=Europe/Berlin:20261022T094500\r\nEND:VEVENT\r\n"
           "BEGIN:VEVENT\r\nUID:a2@x\r\nSUMMARY:Holiday\r\nDTSTART;VALUE=DATE:20261026\r\nEND:VEVENT\r\n"
           "END:VCALENDAR\r\n")
    r = call("POST", "/api/import", {"filename": "a.ics", "content": ics})
    plan = r["plan"]
    creates = [o for o in plan["ops"] if o["op"] == "create"]
    assert len(creates) == 2 and plan["status"] == "preview", "ics creates"
    assert creates[0]["after"]["start"] == "2026-10-22T09:15" and creates[0]["after"]["source"] == "ics", "ics time"
    assert creates[1]["after"]["start"] == "2026-10-26T00:00" and creates[1]["after"]["end"] == "2026-10-27T00:00", "ics allday"
    call("POST", "/api/plan/%s/apply" % plan["id"])
    r = call("POST", "/api/import", {"filename": "a.ics", "content": ics})
    assert len([o for o in r["plan"]["ops"] if o["op"] == "create"]) == 0, "ics dup"

    r = call("POST", "/api/learn")
    b = [x for x in r["beliefs"] if x["key"] == "evening_work"]
    assert b and b[0]["confidence"] >= 0.7 and "8" in b[0]["statement"], "evening belief"
    r = call("POST", "/api/view", {"type": "workweek"})
    assert "2026-10-24" in r["view"]["hidden_counts"], "hidden counts"
    call("DELETE", "/api/beliefs/evening_work")
    r = call("POST", "/api/learn")
    assert not [x for x in r["beliefs"] if x["key"] == "evening_work"], "belief stays deleted"

    seeds2 = []
    base = date(2026, 8, 3)
    for w in range(10):
        dd = base + timedelta(days=7 * w)
        seeds2.append({"id": "w%d" % w, "kind": "task", "title": "Weekly task %d" % w,
                       "start": fmt(at(dd, 10, 0)), "end": fmt(at(dd, 11, 0))})
        if w < 6:
            d2 = dd + timedelta(days=1)
            seeds2.append({"id": "e%d" % w, "kind": "task", "title": "Evening task %d" % w,
                           "start": fmt(at(d2, 20, 30)), "end": fmt(at(d2, 21, 30)), "status": "done"})
    call("POST", "/api/reset", {"now": "2026-10-19T09:00", "items": seeds2})
    r = call("POST", "/api/learn")
    p1 = r["proposal"]
    assert p1 and p1["novelty"] == "invented" and p1["status"] == "shown", "proposal 1"
    assert ST["view"]["type"] == "week", "view unchanged"
    call("POST", "/api/proposals/%s/undo" % p1["id"])
    assert find_proposal(p1["id"])["status"] == "reverted", "reverted"
    call("POST", "/api/clock", {"now": "2026-10-19T22:00"})
    call("POST", "/api/learn")
    p2 = call("GET", "/api/proposal")["proposal"]
    assert p2 and p2["id"] != p1["id"] and p2["novelty"] == "invented", "proposal 2"
    call("POST", "/api/proposals/%s/undo" % p2["id"])
    st = call("GET", "/api/state")
    assert st["meta"]["novelty"] < 0.6, "novelty dropped"
    ds = [x for x in call("GET", "/api/prompts")["prompts"] if x["agent"] == "designer"]
    assert len(ds) >= 2 and ds[-1]["core_hash"] == ds[0]["core_hash"] and ds[-1]["diff"], "designer v2"
    rb = call("POST", "/api/prompts/designer/rollback")
    assert rb["active"]["version"] == 1, "rollback"
    kinds = set(e["kind"] for e in call("GET", "/api/activity")["events"])
    assert set(["belief", "proposal", "prompt"]) <= kinds, "activity kinds"

    call("POST", "/api/reset", {"now": "2026-10-19T09:00", "items": seeds})
    r = call("POST", "/api/clock", {"now": "2026-10-22T23:59"})
    assert any(m["kind"] == "reminder" for m in r["messages"]), "reminders"
    assert item("c1")["postpone_count"] == 2, "c1 postponed twice"
    assert len([m for m in ST["messages"] if m["kind"] == "checkin" and m["item_id"] == "c1"]) == 1, "checkin once"
    posted = [m for m in ST["messages"] if m["kind"] == "postponed"]
    assert posted and posted[0]["undo_id"], "postponed msg"
    call("POST", "/api/undo", {"undo_id": posted[0]["undo_id"]})
    assert item("c1")["start"] == "2026-10-19T10:00", "undo postpone"
    for m in ST["messages"]:
        low = m["text"].lower()
        for bad, _ in BANNED:
            assert bad not in low, "banned word %s in %s" % (bad, m["text"])
    call("GET", "/api/state")
    wipe_state()


# ---------------------------------------------------------------- main


def main():
    global DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default="data")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    DATA_DIR = a.data
    os.makedirs(a.data, exist_ok=True)
    if a.selftest:
        try:
            self_test()
            if os.path.exists(os.path.join(APP_DIR, "tests.py")):
                if APP_DIR not in sys.path:
                    sys.path.insert(0, APP_DIR)
                import tests
                tests.run()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    server = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
