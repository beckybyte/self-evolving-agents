import argparse
import copy
import json
import os
import re
import threading
import uuid
from datetime import datetime, date, time as dtime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import v2
import v3
import commands
import convo

STATE = {
    "now": "2026-10-19T19:50",
    "items": [],
    "messages": [],
    "convo": {},
    "undo": {},
    "sent": {},
    "streak_yes": 0,
    "streak_no": 0,
}

LOCK = threading.Lock()
DATA_DIR = "."

BANNED = ["overdue", "fail", "miss", "late", "behind", "should have", "forgot", "lazy", "urgent", "alert"]
KIND_REPLACEMENTS = {
    "overdue": "waiting",
    "fail": "not work out",
    "miss": "skip",
    "late": "afterwards",
    "behind": "a bit back",
    "should have": "could",
    "forgot": "let slip",
    "lazy": "tired",
    "urgent": "important",
    "alert": "note",
}

ITEM_KEYS = ["id", "kind", "title", "start", "end", "window", "status", "why",
             "parent_id", "estimate_min", "postpone_count", "source"]

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

VAGUE_VERBS = [
    "work on", "figure out", "deal with", "sort out", "sort through", "handle",
    "tackle", "get on top of", "catch up on", "clean out", "clean up",
    "get together", "get done", "get sorted", "organize", "organise", "plan",
    "prepare", "research", "finish", "start on", "apply for", "overhaul",
    "redo", "revamp", "set up", "put together", "go through", "look into",
    "think about", "review", "arrange", "coordinate", "brainstorm", "draft",
    "write up", "sort", "clear", "declutter", "tidy", "fix", "update",
    "reorganize", "reorganise", "streamline", "consolidate", "assemble",
    "compile", "process", "manage", "improve", "refine", "polish", "build",
    "create", "develop", "design", "map out", "plan out", "wrap up",
]

BIG_NOUNS = [
    "project", "portfolio", "pitch deck", "deck", "thesis", "website", "garage",
    "closet", "inbox", "taxes", "application", "claim", "move", "renovation",
    "novel", "kitchen", "basement", "attic", "storage", "paperwork", "budget",
    "resume", "cv", "presentation", "report", "essay", "book", "garden",
    "wedding", "party", "conference", "business", "startup", "career",
    "apartment", "house", "room", "office", "files", "photos", "accounts",
    "finances", "insurance", "visa", "immigration", "course", "degree",
    "dissertation", "manuscript", "album", "archive", "database", "system",
    "workflow", "process", "strategy", "roadmap", "campaign", "launch",
]

WINDOW_WORDS = [
    ("around noon", 12, 0, 13, 0),
    ("noon", 12, 0, 13, 0),
    ("lunch", 12, 0, 13, 0),
    ("this morning", 9, 0, 12, 0),
    ("morning", 9, 0, 12, 0),
    ("this afternoon", 14, 0, 17, 0),
    ("afternoon", 14, 0, 17, 0),
    ("this evening", 18, 0, 21, 0),
    ("evening", 18, 0, 21, 0),
    ("tonight", 20, 0, 22, 0),
]

APPOINTMENT_WORDS = ["with", "meet", "lunch", "dinner", "call with", "coffee", "appointment"]


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

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


def now_dt(S):
    return parse_iso(S.get("now")) or datetime(2026, 10, 19, 19, 50)


def new_id(prefix="i"):
    return prefix + "_" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# wording check
# ---------------------------------------------------------------------------

def kind_text(text):
    if not text:
        return text
    out = text
    low = out.lower()
    for bad in BANNED:
        if bad in low:
            repl = KIND_REPLACEMENTS.get(bad, "gently")
            out = re.sub(re.escape(bad), repl, out, flags=re.IGNORECASE)
            low = out.lower()
    return out


def strip_question(text):
    if not text:
        return text
    return text.replace("?", "").strip()


# ---------------------------------------------------------------------------
# item helpers
# ---------------------------------------------------------------------------

def default_item(raw):
    it = {}
    it["id"] = raw.get("id") or new_id()
    it["kind"] = raw.get("kind") or "task"
    it["title"] = raw.get("title") or "Untitled"
    it["start"] = raw.get("start")
    it["end"] = raw.get("end")
    it["window"] = raw.get("window")
    it["status"] = raw.get("status") or "open"
    it["why"] = raw.get("why")
    it["parent_id"] = raw.get("parent_id")
    it["estimate_min"] = raw.get("estimate_min")
    it["postpone_count"] = raw.get("postpone_count") or 0
    it["source"] = raw.get("source") or "seed"
    for k, v in raw.items():
        if k not in it:
            it[k] = v
    return it


def find_item(S, item_id):
    for it in S["items"]:
        if it.get("id") == item_id:
            return it
    return None


def add_agent(S, kind, text, item_id=None, undo_id=None, steps=None):
    m = {
        "id": "a_" + uuid.uuid4().hex[:10],
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


def add_person(S, text, item_id=None):
    m = {
        "id": "p_" + uuid.uuid4().hex[:10],
        "role": "person",
        "kind": "reply",
        "text": text,
        "item_id": item_id,
        "at": S.get("now"),
        "undo_id": None,
    }
    S["messages"].append(m)
    return m


def make_undo(S, item):
    uid = "u_" + uuid.uuid4().hex[:10]
    S["undo"][uid] = {
        "item_id": item.get("id"),
        "before": copy.deepcopy(item),
        "start": item.get("start"),
        "end": item.get("end"),
        "window": item.get("window"),
        "postpone_count": item.get("postpone_count"),
        "used": False,
    }
    return uid


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def find_date(text, base_dt):
    """Return (date_d, matched_span) or (None, None)."""
    low = text.lower()
    base_d = base_dt.date()

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, m.span()
        except Exception:
            pass

    m = re.search(r"\b(" + "|".join(MONTHS.keys()) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
    if m:
        mon = MONTHS.get(m.group(1))
        day = int(m.group(2))
        if mon:
            year = base_d.year
            try:
                d = date(year, mon, day)
                if d < base_d - timedelta(days=180):
                    d = date(year + 1, mon, day)
                return d, m.span()
            except Exception:
                pass

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS.keys()) + r")\b", low)
    if m:
        mon = MONTHS.get(m.group(2))
        day = int(m.group(1))
        if mon:
            year = base_d.year
            try:
                d = date(year, mon, day)
                if d < base_d - timedelta(days=180):
                    d = date(year + 1, mon, day)
                return d, m.span()
            except Exception:
                pass

    if re.search(r"\btoday\b", low):
        m = re.search(r"\btoday\b", low)
        return base_d, m.span()
    if re.search(r"\btomorrow\b", low):
        m = re.search(r"\btomorrow\b", low)
        return base_d + timedelta(days=1), m.span()

    for name in sorted(WEEKDAYS.keys(), key=len, reverse=True):
        m = re.search(r"\b" + name + r"\b", low)
        if m:
            target = WEEKDAYS[name]
            delta = (target - base_d.weekday()) % 7
            if delta == 0:
                delta = 7
            return base_d + timedelta(days=delta), m.span()

    return None, None


def find_clock(text):
    """Return (hour, minute, span) or (None, None, None)."""
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
            return h, mi, m.span()
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
    if m:
        h = int(m.group(1))
        ap = m.group(2)
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return h, 0, m.span()
    m = re.search(r"\bat\s+(\d{1,2})\b", low)
    if m:
        h = int(m.group(1))
        if 1 <= h <= 7:
            h += 12
        if 0 <= h <= 23:
            return h, 0, m.span()
    return None, None, None


def find_window(text):
    low = text.lower()
    for phrase, h1, m1, h2, m2 in WINDOW_WORDS:
        idx = low.find(phrase)
        if idx >= 0:
            return phrase, h1, m1, h2, m2, (idx, idx + len(phrase))
    return None, None, None, None, None, None


def clean_title(text):
    t = text.strip()
    t = re.sub(r"^(maybe|perhaps|also|and)\s+", "", t, flags=re.IGNORECASE)
    t = t.replace("?", "").strip()
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return "Untitled"
    return t[0].upper() + t[1:]


def parse_fragment(text, base_dt):
    """Parse one fragment into an item dict (without id)."""
    raw = text.strip()
    if not raw:
        return None
    low = raw.lower()

    date_d, date_span = find_date(raw, base_dt)
    work = raw
    if date_span:
        work = raw[:date_span[0]] + " " + raw[date_span[1]:]

    h, mi, clock_span = find_clock(work)
    wphrase, wh1, wm1, wh2, wm2, wspan = find_window(work)

    title_src = work
    if clock_span:
        title_src = title_src[:clock_span[0]] + " " + title_src[clock_span[1]:]
    if wspan:
        title_src = title_src[:wspan[0]] + " " + title_src[wspan[1]:]
    title = clean_title(title_src)

    item = {
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
        "source": "typed",
    }

    if h is not None:
        if date_d is None:
            cand = datetime.combine(base_dt.date(), dtime(h, mi))
            if cand <= base_dt:
                date_d = base_dt.date() + timedelta(days=1)
            else:
                date_d = base_dt.date()
        start_dt = datetime.combine(date_d, dtime(h, mi))
        item["kind"] = "event"
        item["start"] = fmt_iso(start_dt)
        item["end"] = fmt_iso(start_dt + timedelta(minutes=60))
        item["window"] = None
        return item

    if wphrase is not None:
        if date_d is None:
            date_d = base_dt.date()
        start_dt = datetime.combine(date_d, dtime(wh1, wm1))
        end_dt = datetime.combine(date_d, dtime(wh2, wm2))
        item["window"] = wphrase
        item["start"] = fmt_iso(start_dt)
        item["end"] = fmt_iso(end_dt)
        is_appt = any(w in low for w in APPOINTMENT_WORDS)
        item["kind"] = "event" if is_appt else "task"
        return item

    if date_d is not None:
        item["start"] = fmt_iso(datetime.combine(date_d, dtime(9, 0)))
        item["end"] = None
        item["kind"] = "task"
        return item

    if low.startswith("maybe") or raw.strip().endswith("?"):
        item["kind"] = "note"
    else:
        item["kind"] = "task"
    return item


def is_big(text, item):
    if item and item.get("estimate_min") and item["estimate_min"] > 120:
        return True
    low = (text or "").lower()
    for v in VAGUE_VERBS:
        if re.search(r"\b" + re.escape(v) + r"\b", low):
            return True
    for n in BIG_NOUNS:
        if re.search(r"\b" + re.escape(n) + r"\b", low):
            return True
    return False


# ---------------------------------------------------------------------------
# reset / state
# ---------------------------------------------------------------------------

def do_reset(body):
    now = body.get("now") or "2026-10-19T19:50"
    seed = body.get("items") or []
    STATE["now"] = now
    STATE["items"] = [default_item(x) for x in seed]
    STATE["messages"] = []
    STATE["convo"] = {}
    STATE["undo"] = {}
    STATE["sent"] = {}
    STATE["streak_yes"] = 0
    STATE["streak_no"] = 0
    try:
        v2.init_v2(STATE)
    except Exception:
        pass
    try:
        convo.reset(STATE)
    except Exception:
        pass
    add_agent(STATE, "info", "Hi, I'm here whenever you want to plan a little.")
    return state_payload()


def state_payload():
    out = {
        "now": STATE.get("now"),
        "items": STATE.get("items", []),
        "messages": STATE.get("messages", []),
    }
    try:
        out["view"] = v2.view_state(STATE)
    except Exception:
        out["view"] = {"type": "week", "params": {}, "hidden_counts": {}}
    try:
        out["meta"] = v2.meta_state(STATE)
    except Exception:
        out["meta"] = {}
    return out


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------

def do_command(body):
    text = body.get("text") or ""
    mode = body.get("mode") or "command"
    new_messages = []
    created_ids = []

    if mode == "command":
        try:
            r = commands.handle_command(STATE, text)
        except Exception:
            r = None
        if r is not None:
            return r

    if mode == "dump":
        fragments = re.split(r"[,;\n]+", text)
        fragments = [f.strip() for f in fragments if f.strip()]
        base_dt = now_dt(STATE)
        first_big = None
        titles = []
        for frag in fragments:
            parsed = parse_fragment(frag, base_dt)
            if not parsed:
                continue
            parsed["id"] = new_id()
            parsed["source"] = "dump"
            item = default_item(parsed)
            STATE["items"].append(item)
            created_ids.append(item["id"])
            titles.append(strip_question(item["title"]))
            try:
                v2.add_log(STATE, "create", [item["id"]], {"title": item["title"]}, "dump")
            except Exception:
                pass
            if first_big is None and is_big(frag, item):
                first_big = item
        if titles:
            listing = ", ".join("'" + t + "'" for t in titles)
            new_messages.append(add_agent(STATE, "info", "Added " + listing + "."))
        else:
            new_messages.append(add_agent(STATE, "info", "I didn't catch anything to add."))
        if first_big is not None:
            try:
                extra = convo.on_big_task(STATE, first_big)
                if extra:
                    new_messages.extend(extra)
            except Exception:
                pass
        return {"messages": new_messages, "created": created_ids}

    # single command
    base_dt = now_dt(STATE)
    parsed = parse_fragment(text, base_dt)
    if not parsed:
        new_messages.append(add_agent(STATE, "info", "I didn't catch that. Try telling me one thing."))
        return {"messages": new_messages, "created": []}
    parsed["id"] = new_id()
    parsed["source"] = "typed"
    item = default_item(parsed)
    STATE["items"].append(item)
    created_ids.append(item["id"])
    try:
        v2.add_log(STATE, "create", [item["id"]], {"title": item["title"]}, "typed")
    except Exception:
        pass
    new_messages.append(add_agent(STATE, "info", "Added '" + strip_question(item["title"]) + "'."))
    if is_big(text, item):
        try:
            extra = convo.on_big_task(STATE, item)
            if extra:
                new_messages.extend(extra)
        except Exception:
            pass
    return {"messages": new_messages, "created": created_ids}


# ---------------------------------------------------------------------------
# done / undo / focus
# ---------------------------------------------------------------------------

def do_done(item_id):
    item = find_item(STATE, item_id)
    if item is None:
        raise KeyError(item_id)
    item["status"] = "done"
    try:
        v2.add_log(STATE, "done", [item_id], {"title": item.get("title")}, "person")
    except Exception:
        pass
    return {"item": item}


def do_undo(body):
    uid = body.get("undo_id")
    rec = STATE.get("undo", {}).get(uid)
    if not rec:
        return {"ok": True}
    if rec.get("used"):
        return {"ok": True}
    item_id = rec.get("item_id")
    before = rec.get("before")
    items = STATE["items"]
    idx = None
    for i, it in enumerate(items):
        if it.get("id") == item_id:
            idx = i
            break
    if before is not None:
        if idx is not None:
            items[idx] = copy.deepcopy(before)
        else:
            items.append(copy.deepcopy(before))
    else:
        if idx is not None:
            for k in ("start", "end", "window", "postpone_count"):
                items[idx][k] = rec.get(k)
    rec["used"] = True
    try:
        v2.add_log(STATE, "update", [item_id] if item_id else [], {"undo": uid}, "person")
    except Exception:
        pass
    return {"ok": True}


def do_focus():
    items = STATE.get("items", [])
    open_items = [it for it in items if it.get("status") == "open"]
    goals = [it for it in open_items if it.get("why")]
    goal_ids = set(g.get("id") for g in goals)
    children = [it for it in open_items if it.get("parent_id") in goal_ids]

    def sort_key(it):
        s = it.get("start")
        return (0, s) if s else (1, "")

    if children:
        children_sorted = sorted(children, key=sort_key)
        chosen = children_sorted[0]
        parent = find_item(STATE, chosen.get("parent_id"))
        why = parent.get("why") if parent else None
        return {"item": chosen, "parent": parent, "why": why, "action": "Start 5 minutes"}

    parent_ids = set(it.get("id") for it in items)
    has_children = set(it.get("parent_id") for it in items if it.get("parent_id"))
    candidates = [it for it in open_items if it.get("id") not in has_children]
    if not candidates:
        return {"item": None, "parent": None, "why": None, "action": "Start 5 minutes"}
    candidates_sorted = sorted(candidates, key=sort_key)
    chosen = candidates_sorted[0]
    parent = None
    if chosen.get("parent_id"):
        parent = find_item(STATE, chosen.get("parent_id"))
    why = None
    if parent and parent.get("why"):
        why = parent.get("why")
    elif chosen.get("why"):
        why = chosen.get("why")
    return {"item": chosen, "parent": parent, "why": why, "action": "Start 5 minutes"}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

BUILTIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Evolving Calendar</title></head>
<body>
<h1>Evolving Calendar</h1>
<div id="focus">Right now, just this</div>
<div id="timeline">Your calendar</div>
<div id="chat-log"></div>
<input id="chat-input" placeholder="Tell me one thing">
<button id="send">Send</button>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        try:
            data = json.dumps(obj).encode("utf-8")
        except Exception:
            data = json.dumps({"error": "serialization"}).encode("utf-8")
            status = 500
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            pass

    def _send_html(self, text, status=200):
        data = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
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
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        with LOCK:
            try:
                self._handle_get()
            except KeyError as e:
                self._send_json({"error": "not found: " + str(e)}, 404)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

    def do_POST(self):
        with LOCK:
            try:
                self._handle_post()
            except KeyError as e:
                self._send_json({"error": "not found: " + str(e)}, 404)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        with LOCK:
            try:
                self._handle_delete()
            except KeyError as e:
                self._send_json({"error": "not found: " + str(e)}, 404)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

    def _handle_get(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            here = os.path.dirname(os.path.abspath(__file__))
            idx = os.path.join(here, "index.html")
            if os.path.exists(idx):
                try:
                    with open(idx, "r", encoding="utf-8") as f:
                        self._send_html(f.read())
                    return
                except Exception:
                    pass
            self._send_html(BUILTIN_PAGE)
            return
        if path == "/health":
            self._send_json({"ok": True})
            return
        if path == "/api/state":
            self._send_json(state_payload())
            return
        if path == "/api/focus":
            self._send_json(do_focus())
            return
        if path == "/api/log":
            self._send_json(v2.get_log(STATE))
            return
        if path == "/api/beliefs":
            self._send_json(v2.get_beliefs(STATE))
            return
        if path == "/api/proposal":
            self._send_json(v2.get_proposal(STATE))
            return
        if path == "/api/prompts":
            self._send_json(v2.get_prompts(STATE))
            return
        if path == "/api/activity":
            self._send_json(v2.get_activity(STATE))
            return
        if path == "/api/catalog":
            self._send_json(v3.get_catalog(STATE))
            return
        self._send_json({"error": "unknown path"}, 404)

    def _handle_post(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/reset":
            self._send_json(do_reset(body))
            return
        if path == "/api/command":
            self._send_json(do_command(body))
            return
        if path == "/api/chat":
            item_id = body.get("item_id")
            text = body.get("text", "")
            self._send_json(convo.chat(STATE, item_id, text))
            return
        if path == "/api/clock":
            self._send_json(convo.clock(STATE, body.get("now")))
            return
        if path == "/api/undo":
            self._send_json(do_undo(body))
            return
        if path == "/api/import":
            self._send_json(commands.import_file(STATE, body.get("filename", ""), body.get("content", "")))
            return
        if path == "/api/learn":
            self._send_json(v2.learn(STATE))
            return
        if path == "/api/view":
            self._send_json(v2.set_view(STATE, body.get("type")))
            return
        if path == "/api/critic":
            self._send_json(v3.critic(STATE, body.get("spec")))
            return
        if path == "/api/design":
            r = v3.design(STATE, body.get("belief_key"))
            try:
                r["proposal"] = v2.learn(STATE)["proposal"]
            except Exception:
                r["proposal"] = None
            self._send_json(r)
            return

        m = re.match(r"^/api/items/([^/]+)/done$", path)
        if m:
            self._send_json(do_done(unquote(m.group(1))))
            return
        m = re.match(r"^/api/plan/([^/]+)/apply$", path)
        if m:
            self._send_json(commands.apply_plan(STATE, unquote(m.group(1))))
            return
        m = re.match(r"^/api/plan/([^/]+)/undo$", path)
        if m:
            self._send_json(commands.undo_plan(STATE, unquote(m.group(1))))
            return
        m = re.match(r"^/api/proposals/([^/]+)/([^/]+)$", path)
        if m:
            self._send_json(v2.proposal_action(STATE, unquote(m.group(1)), unquote(m.group(2))))
            return
        m = re.match(r"^/api/prompts/([^/]+)/rollback$", path)
        if m:
            self._send_json(v2.rollback(STATE, unquote(m.group(1))))
            return

        self._send_json({"error": "unknown path"}, 404)

    def _handle_delete(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/beliefs/([^/]+)$", path)
        if m:
            self._send_json(v2.delete_belief(STATE, unquote(m.group(1))))
            return
        self._send_json({"error": "unknown path"}, 404)


def main():
    global DATA_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default="./data")
    args = parser.parse_args()
    DATA_DIR = args.data
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
